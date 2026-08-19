"""The typed output contract for the classifier agent.

Every field here is one downstream code reads. That is the test for whether a
field belongs in a structured output: if nothing consumes it programmatically,
it is prose wearing a JSON costume and it belongs in a `notes` string.
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator

# The closed vocabulary of anomaly classes. Closed, and small, because an open
# label space cannot be evaluated: you cannot measure accuracy against a
# category the model invented on this one call.
AnomalyClass = Literal[
    "thermal_excursion",
    "vacuum_integrity",
    "tooling_wear",
    "material_defect",
    "fastener_process",
    "sensor_or_comms",
    "calibration",
    "unknown",
]

Disposition = Literal[
    "continue_production",   # anomaly is cosmetic or self-clearing
    "hold_station",          # stop this station, line keeps running
    "hold_lot",              # quarantine the affected units
    "stop_line",             # everything stops
]


class Classification(BaseModel):
    """What the classifier agent must return. Nothing else is accepted."""

    model_config = {"extra": "forbid"}  # an unexpected key is a rejection

    anomaly_class: AnomalyClass
    severity: int = Field(ge=0, le=4)

    # The station whose behaviour triggered this. Usually the event's station,
    # but not always — a vacuum loss at S00 can present as a void at S03, and
    # the classifier is allowed to say so.
    root_station: str

    # Free text, and deliberately the ONLY free text field. Bounded so a model
    # cannot pay for a low confidence score with a wall of hedging.
    rationale: str = Field(min_length=20, max_length=600)

    # Calibrated confidence. Read by the routing policy, so it has to be a
    # number with a consistent meaning rather than a vibe.
    confidence: float = Field(ge=0.0, le=1.0)

    proposed_disposition: Disposition

    # Which fields the model felt were missing. This is the honest-uncertainty
    # channel: a model that says "I could not see the thermocouple trace" is
    # more useful than one that guesses and is confidently wrong.
    missing_inputs: list[str] = Field(default_factory=list, max_length=6)

    @field_validator("root_station")
    @classmethod
    def station_is_known(cls, v: str) -> str:
        from desk.common.stations import STATIONS
        if v not in STATIONS:
            raise ValueError(f"unknown station {v!r}")
        return v

    @field_validator("rationale")
    @classmethod
    def rationale_is_not_a_restatement(cls, v: str) -> str:
        # Cheap guard against the model echoing the prompt back. Not a quality
        # measure, just a floor: a rationale that is only the trigger text
        # carries no information the task row did not already have.
        if v.strip().lower().startswith("the station reported"):
            raise ValueError("rationale restates the trigger without analysis")
        return v


class ValidationFailure(Exception):
    """Raised when the model's output cannot be made into a Classification.

    Deliberately an exception rather than a None return. A caller that forgets
    to check a None gets a confusing failure three frames later; a caller that
    forgets to catch this gets a stack trace pointing at the actual problem.
    """

    def __init__(self, message: str, raw: str, attempt: int) -> None:
        super().__init__(message)
        self.raw = raw
        self.attempt = attempt
