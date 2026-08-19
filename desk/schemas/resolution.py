"""The drafting agent's output contract."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from desk.schemas.citation import Citation
from desk.schemas.triage import Disposition

ActionKind = Literal[
    "hold_station",
    "quarantine_lot",
    "dispatch_technician",
    "schedule_calibration",
    "notify_only",
]


class ProposedAction(BaseModel):
    """One action the agent believes should be taken.

    PROPOSED. The drafter never executes a write; it produces this, and the
    routing layer decides whether it runs, and whether a human sees it first.
    Keeping proposal and execution in different modules is what makes 'the
    agent did something nobody approved' structurally impossible rather than
    merely unlikely.
    """

    model_config = {"extra": "forbid"}

    kind: ActionKind
    target: str                    # station id, lot id, or technician queue
    reason: str = Field(min_length=15, max_length=300)
    # Whether this action can be undone by another action. Read by the routing
    # policy: irreversible actions never auto-apply regardless of confidence.
    reversible: bool


class Resolution(BaseModel):
    model_config = {"extra": "forbid"}

    summary: str = Field(min_length=40, max_length=1200)
    disposition: Disposition
    actions: list[ProposedAction] = Field(max_length=4)
    citations: list[Citation] = Field(min_length=1, max_length=6)
    confidence: float = Field(ge=0.0, le=1.0)
    # What the agent could not determine. Surfaced verbatim to the operator,
    # because the fastest way to make a human useful is to tell them exactly
    # which question to answer.
    open_questions: list[str] = Field(default_factory=list, max_length=4)
