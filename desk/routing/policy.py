"""Decide what happens to a completed triage.

Three outcomes. AUTO_APPLY runs the proposed actions. REVIEW puts it on the
operator's desk with the draft pre-filled. ESCALATE pages someone.

The interesting property of this module is that it is pure. Given a draft, a
classification, and a calibration, it returns a route with no side effects and
no I/O, which means the entire policy is unit-testable without a database, and
the eval harness can replay every historical triage through a candidate policy
and count what would have changed.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass

from desk.agents.drafter import DraftResult
from desk.common.events import Severity
from desk.retrieval.grounding import grounding_rate
from desk.routing.calibration import Calibration
from desk.schemas.triage import Classification


class Route(enum.Enum):
    AUTO_APPLY = "auto_apply"
    REVIEW = "review"
    ESCALATE = "escalate"


@dataclass(frozen=True)
class RoutingDecision:
    route: Route
    # Every reason that applied, not just the first one that matched. An
    # operator asking "why did this escalate" deserves the whole answer, and a
    # first-match-wins implementation hides the other three problems.
    reasons: tuple[str, ...]
    calibrated_confidence: float


# The auto-apply bar, expressed in OBSERVED accuracy rather than stated
# confidence. 0.97 because the cost of a wrong automatic hold is a stopped
# station, and at roughly two hundred triages a week a 0.97 bar still means
# about six wrong automatic actions a week. That number was agreed with the
# operations lead, in advance, in writing.
AUTO_APPLY_MIN_OBSERVED = 0.97

# Grounding floor for any automated action. A resolution acting on unverified
# citations is acting on text nobody has checked.
AUTO_APPLY_MIN_GROUNDING = 1.0


def route(draft: DraftResult, classification: Classification,
          calibration: Calibration) -> RoutingDecision:
    observed = calibration.observed(draft.resolution.confidence)
    grounding = grounding_rate(draft.grounding)
    reasons: list[str] = []

    # Escalation conditions first, and they are absolute. Nothing below can
    # downgrade an escalation, which is why they are evaluated separately
    # rather than as the top of a score.
    if classification.severity >= Severity.CRITICAL:
        reasons.append("severity is critical")
    if draft.resolution.disposition == "stop_line":
        reasons.append("proposed disposition stops the line")
    if classification.anomaly_class == "material_defect":
        # A material defect is a quality escape risk. It goes to a human every
        # time regardless of how confident anything is, because the failure is
        # not recoverable downstream and the regulatory posture requires a
        # named person on the decision.
        reasons.append("material defect requires a named human decision")
    if reasons:
        return RoutingDecision(Route.ESCALATE, tuple(reasons), observed)

    if draft.partial:
        reasons.append("agent returned a partial result")
    if grounding < AUTO_APPLY_MIN_GROUNDING:
        reasons.append(f"grounding rate {grounding:.2f} below 1.00")
    if observed < AUTO_APPLY_MIN_OBSERVED:
        reasons.append(f"calibrated confidence {observed:.2f} below "
                       f"{AUTO_APPLY_MIN_OBSERVED:.2f}")
    if draft.resolution.open_questions:
        reasons.append(f"{len(draft.resolution.open_questions)} open questions")
    if any(not a.reversible for a in draft.resolution.actions):
        # Reversibility is a routing input in its own right. A reversible
        # action taken wrongly costs the time to undo it. An irreversible one
        # costs whatever it cost. No confidence number should be able to buy
        # past that asymmetry.
        reasons.append("proposes an irreversible action")
    if not calibration.is_monotone():
        reasons.append("calibration is not monotone; confidence is unusable")

    if reasons:
        return RoutingDecision(Route.REVIEW, tuple(reasons), observed)
    return RoutingDecision(
        Route.AUTO_APPLY,
        ("calibrated confidence and grounding both clear the bar",),
        observed,
    )
