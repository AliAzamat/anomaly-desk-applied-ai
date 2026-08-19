"""Latency and cost, by stage, over a window.

Percentiles rather than means throughout. A mean latency of 6 seconds on a
workflow whose p99 is 45 seconds describes a system nobody experiences: the
operators who notice are the ones in the tail, and the mean is specifically
constructed to hide them.
"""
from __future__ import annotations

import json
import statistics
from dataclasses import dataclass

from desk.store import db


@dataclass
class StageStats:
    stage: str
    n: int
    p50_ms: float
    p95_ms: float
    p99_ms: float
    mean_cost_usd: float
    # Total spend, which is what finance asks about, kept separate from the
    # per-triage figure, which is what tells you whether a change helped.
    total_cost_usd: float

    @property
    def tail_ratio(self) -> float:
        """p99 over p50. Above roughly 5 means a bimodal distribution, which
        almost always means two different code paths and not one slow one."""
        return self.p99_ms / self.p50_ms if self.p50_ms else 0.0


def percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    # Nearest-rank. Simple, and for latency reporting the interpolation
    # question is far less important than people arguing about it think.
    index = min(int(q * len(ordered)), len(ordered) - 1)
    return ordered[index]


def stage_stats(window_hours: int = 24) -> list[StageStats]:
    with db.conn() as c:
        rows = c.execute(
            """
            SELECT payload FROM trace_span
            WHERE created_at >= now() - make_interval(hours => %s)
            """,
            (window_hours,),
        ).fetchall()

    durations: dict[str, list[float]] = {}
    costs: dict[str, list[float]] = {}
    for row in rows:
        payload = row["payload"]
        for child in payload.get("children", []):
            stage = child["name"].split(".")[0]
            durations.setdefault(stage, []).append(child["duration_ms"])
            costs.setdefault(stage, []).append(
                _subtree_cost(child))

    out: list[StageStats] = []
    for stage, values in durations.items():
        stage_costs = costs.get(stage, [0.0])
        out.append(StageStats(
            stage=stage,
            n=len(values),
            p50_ms=percentile(values, 0.50),
            p95_ms=percentile(values, 0.95),
            p99_ms=percentile(values, 0.99),
            mean_cost_usd=statistics.fmean(stage_costs),
            total_cost_usd=sum(stage_costs),
        ))
    # Sorted by total spend. The stage at the top is where a cost reduction
    # has leverage; everything below it is rounding.
    out.sort(key=lambda s: s.total_cost_usd, reverse=True)
    return out


def _subtree_cost(node: dict) -> float:
    return (node["attributes"].get("cost_usd", 0.0)
            + sum(_subtree_cost(c) for c in node.get("children", [])))


def render(stats: list[StageStats]) -> str:
    lines = ["stage           n      p50      p95      p99   tail   $/triage   $ total"]
    for s in stats:
        lines.append(
            f"{s.stage:<14} {s.n:>5} {s.p50_ms:>8.0f} {s.p95_ms:>8.0f} "
            f"{s.p99_ms:>8.0f} {s.tail_ratio:>6.1f} "
            f"{s.mean_cost_usd:>10.4f} {s.total_cost_usd:>9.2f}"
        )
    return "\n".join(lines)
