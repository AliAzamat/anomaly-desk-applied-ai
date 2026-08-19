"""Put the judge score and the override rate next to each other.

This module computes almost nothing. Its whole purpose is to render two numbers
in one place, per anomaly class, so their disagreement is impossible to look
away from.

The judge score comes from a frozen eval set graded offline. The override rate
comes from production. When they diverge, exactly one of the following is true,
and the report exists to help you work out which:

  1. The eval set does not represent production traffic.
  2. The graders measure something other than what operators care about.
  3. Operators are overriding for reasons unrelated to correctness.
  4. The judge is wrong, and its agreement number is stale.

Note that only ONE of those four is a model problem.
"""
from __future__ import annotations

from dataclasses import dataclass

from desk.store import db


@dataclass
class ScoreboardRow:
    anomaly_class: str
    judge_score: float           # from the frozen eval set
    judge_n: int
    override_rate: float         # from production, reviewed triages only
    reviewed_n: int
    # The top override categories for this class, which is where the
    # explanation for a divergence usually is.
    top_override_reasons: list[tuple[str, int]]

    @property
    def gap(self) -> float:
        """Judge score minus agent-correct rate in production."""
        return self.judge_score - (1.0 - self.override_rate)


def build(eval_scores: dict[str, tuple[float, int]],
          window_days: int = 30) -> list[ScoreboardRow]:
    with db.conn() as c:
        prod = c.execute(
            """
            SELECT d.anomaly_class,
                   count(*) AS reviewed,
                   count(o.override_id) AS overridden
            FROM triage_decision d
            LEFT JOIN operator_override o ON o.task_id = d.task_id
            WHERE d.reviewed_by_human = TRUE
              AND d.decided_at >= now() - make_interval(days => %s)
            GROUP BY d.anomaly_class
            """,
            (window_days,),
        ).fetchall()

        reasons = c.execute(
            """
            SELECT d.anomaly_class, o.category, count(*) AS n
            FROM operator_override o
            JOIN triage_decision d ON d.task_id = o.task_id
            WHERE o.created_at >= now() - make_interval(days => %s)
            GROUP BY d.anomaly_class, o.category
            ORDER BY d.anomaly_class, n DESC
            """,
            (window_days,),
        ).fetchall()

    by_class: dict[str, list[tuple[str, int]]] = {}
    for row in reasons:
        by_class.setdefault(row["anomaly_class"], []).append(
            (row["category"], row["n"]))

    rows: list[ScoreboardRow] = []
    for record in prod:
        anomaly_class = record["anomaly_class"]
        judge_score, judge_n = eval_scores.get(anomaly_class, (0.0, 0))
        reviewed = record["reviewed"]
        rows.append(ScoreboardRow(
            anomaly_class=anomaly_class,
            judge_score=judge_score,
            judge_n=judge_n,
            override_rate=record["overridden"] / reviewed if reviewed else 0.0,
            reviewed_n=reviewed,
            top_override_reasons=by_class.get(anomaly_class, [])[:3],
        ))
    # Sorted by the gap, largest first. The ordering IS the priority list:
    # the class where the two scoreboards disagree most is the class where
    # your understanding of the system is most wrong.
    rows.sort(key=lambda r: abs(r.gap), reverse=True)
    return rows


def render(rows: list[ScoreboardRow]) -> str:
    lines = ["class                judge   prod-ok   gap    n(eval)  n(prod)  top override"]
    for row in rows:
        prod_ok = 1.0 - row.override_rate
        top = row.top_override_reasons[0][0] if row.top_override_reasons else "-"
        lines.append(
            f"{row.anomaly_class:<20} {row.judge_score:>5.2f}   "
            f"{prod_ok:>5.2f}  {row.gap:>+5.2f}   "
            f"{row.judge_n:>5}   {row.reviewed_n:>5}   {top}"
        )
    return "\n".join(lines)
