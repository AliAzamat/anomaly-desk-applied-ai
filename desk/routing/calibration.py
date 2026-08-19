"""Map a model's stated confidence to observed accuracy.

The classifier says 0.9. The question this module answers is: historically,
when it said 0.9, how often was it right?

Built from the override table, which means it is computed from what operators
actually did rather than from anything the model asserts about itself.
"""
from __future__ import annotations

import bisect
from dataclasses import dataclass

from desk.store import db

# Bucket edges. Coarse deliberately — fine buckets look precise and are noise
# when each holds nine samples. Ten buckets over a few thousand triages is
# about the finest granularity the data supports.
EDGES = [0.0, 0.5, 0.6, 0.7, 0.8, 0.85, 0.9, 0.95, 0.98, 1.01]

# Below this many samples a bucket does not get its own estimate; it inherits
# the pooled rate. Without this, a bucket with two lucky samples reports 1.0
# and the router starts auto-applying on it.
MIN_BUCKET_SAMPLES = 40


@dataclass(frozen=True)
class Bucket:
    lo: float
    hi: float
    samples: int
    observed_accuracy: float
    pooled: bool          # True when this bucket fell back to the global rate


class Calibration:
    def __init__(self, buckets: list[Bucket], pooled_rate: float) -> None:
        self._buckets = buckets
        self._pooled = pooled_rate
        self._los = [b.lo for b in buckets]

    def observed(self, stated: float) -> float:
        """Observed accuracy for the bucket a stated confidence falls into."""
        if not self._buckets:
            # No calibration data yet. Return the pooled rate rather than the
            # stated confidence — an uncalibrated system must not inherit the
            # model's optimism as if it were measured.
            return self._pooled
        index = bisect.bisect_right(self._los, stated) - 1
        index = max(0, min(index, len(self._buckets) - 1))
        return self._buckets[index].observed_accuracy

    def is_monotone(self) -> bool:
        """True when observed accuracy rises with stated confidence.

        This is the property that makes confidence usable at all. If the 0.9
        bucket is no more accurate than the 0.6 bucket, the field carries no
        information and any routing built on it is routing on noise. The gate
        checks this before allowing a confidence-based auto-apply threshold.
        """
        real = [b for b in self._buckets if not b.pooled and b.samples > 0]
        if len(real) < 3:
            return False
        return all(a.observed_accuracy <= b.observed_accuracy + 0.02
                   for a, b in zip(real, real[1:]))


def build(window_days: int = 60) -> Calibration:
    """Compute calibration from operator decisions in the recent window.

    Recent, because calibration drifts. A model version change, a prompt edit,
    or a shift in the anomaly mix all move the mapping, and a calibration
    computed over all history averages the current model with two retired ones.
    """
    with db.conn() as c:
        rows = c.execute(
            """
            SELECT d.agent_confidence AS stated,
                   (o.override_id IS NULL) AS agent_was_right
            FROM triage_decision d
            LEFT JOIN operator_override o ON o.task_id = d.task_id
            WHERE d.decided_at >= now() - make_interval(days => %s)
              AND d.reviewed_by_human = TRUE
            """,
            (window_days,),
        ).fetchall()

    if not rows:
        return Calibration([], pooled_rate=0.5)

    pooled = sum(1 for r in rows if r["agent_was_right"]) / len(rows)
    buckets: list[Bucket] = []
    for lo, hi in zip(EDGES, EDGES[1:]):
        in_bucket = [r for r in rows if lo <= r["stated"] < hi]
        if len(in_bucket) < MIN_BUCKET_SAMPLES:
            buckets.append(Bucket(lo, hi, len(in_bucket), pooled, pooled=True))
            continue
        accuracy = sum(1 for r in in_bucket if r["agent_was_right"]) / len(in_bucket)
        buckets.append(Bucket(lo, hi, len(in_bucket), accuracy, pooled=False))
    return Calibration(buckets, pooled)
