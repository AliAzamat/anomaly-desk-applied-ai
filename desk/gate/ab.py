"""A/B a candidate against production, measured on the override rate.

The judge score is a proxy. This is not. Operators are the ground truth and
their override rate is what the business feels, which is why the experiment
that decides a rollout measures that and not the eval score.

What this module is careful about: an override-rate difference of two points
over a week is almost always noise, and the temptation to call it a win is
enormous when someone has been working on the change for a month.
"""
from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass

from desk.store import db


def assign_arm(task_id: str, experiment: str, treatment_frac: float) -> str:
    """Deterministic assignment by hash of (task, experiment).

    Hashed rather than random so a task's arm is stable across retries and
    recomputation. Including the experiment name means two concurrent
    experiments do not assign correlated arms, which would make each one's
    result contaminated by the other.
    """
    digest = hashlib.sha256(f"{experiment}:{task_id}".encode()).hexdigest()
    return "treatment" if int(digest[:8], 16) % 10000 < treatment_frac * 10000 \
        else "control"


@dataclass
class ArmStats:
    arm: str
    reviewed: int
    overridden: int

    @property
    def override_rate(self) -> float:
        return self.overridden / self.reviewed if self.reviewed else 0.0


@dataclass
class ABResult:
    control: ArmStats
    treatment: ArmStats
    difference: float
    standard_error: float
    z_score: float
    significant: bool
    # How many more reviewed triages per arm are needed to detect the observed
    # difference, when it is not yet significant. This is the number that
    # prevents the conversation from being about whether to ship on a hunch.
    additional_needed: int

    def summary(self) -> str:
        return (
            f"control  {self.control.override_rate:.3f} "
            f"(n={self.control.reviewed})\n"
            f"treatment {self.treatment.override_rate:.3f} "
            f"(n={self.treatment.reviewed})\n"
            f"diff {self.difference:+.3f}  z={self.z_score:+.2f}  "
            f"{'SIGNIFICANT' if self.significant else 'not significant'}\n"
            + ("" if self.significant
               else f"need ~{self.additional_needed} more per arm")
        )


def analyze(experiment: str, alpha_z: float = 1.96) -> ABResult:
    with db.conn() as c:
        rows = c.execute(
            """
            SELECT e.arm,
                   count(*) AS reviewed,
                   count(o.override_id) AS overridden
            FROM experiment_assignment e
            JOIN triage_decision d ON d.task_id = e.task_id
            LEFT JOIN operator_override o ON o.task_id = d.task_id
            WHERE e.experiment = %s
              AND d.reviewed_by_human = TRUE
            GROUP BY e.arm
            """,
            (experiment,),
        ).fetchall()

    stats = {r["arm"]: ArmStats(r["arm"], r["reviewed"], r["overridden"])
             for r in rows}
    control = stats.get("control", ArmStats("control", 0, 0))
    treatment = stats.get("treatment", ArmStats("treatment", 0, 0))

    difference = treatment.override_rate - control.override_rate
    pooled_n = control.reviewed + treatment.reviewed
    if control.reviewed == 0 or treatment.reviewed == 0:
        return ABResult(control, treatment, difference, 0.0, 0.0, False,
                        additional_needed=400)

    pooled_rate = ((control.overridden + treatment.overridden) / pooled_n)
    standard_error = math.sqrt(
        pooled_rate * (1 - pooled_rate)
        * (1 / control.reviewed + 1 / treatment.reviewed))
    z = difference / standard_error if standard_error else 0.0
    significant = abs(z) >= alpha_z

    # Sample size needed per arm to detect the OBSERVED effect at this alpha
    # and 80% power. Reported when the result is not yet significant, because
    # "not significant" invites "so it's the same", and the honest answer is
    # "we cannot tell yet, and here is how much longer it takes".
    if difference and not significant:
        needed = math.ceil(
            (2 * pooled_rate * (1 - pooled_rate) * (alpha_z + 0.84) ** 2)
            / (difference ** 2))
        additional = max(0, needed - min(control.reviewed, treatment.reviewed))
    else:
        additional = 0

    return ABResult(control, treatment, difference, standard_error, z,
                    significant, additional)
