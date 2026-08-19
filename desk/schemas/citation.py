"""The citation type.

A citation is not a string. It is a claim about a specific range of characters
in a specific revision of a specific document, and every part of that is
checkable. Modelling it as a string is how you end up with 'per SPX-MP-4412'
appended to a paragraph the document does not support.
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field, model_validator

SourceKind = Literal["procedure", "incident"]


class Citation(BaseModel):
    model_config = {"extra": "forbid"}

    kind: SourceKind
    source_id: str          # doc_id for procedure, incident_id for incident
    # For procedures only: the revision the agent read. A citation to a
    # superseded revision is a real failure mode, not a formatting nit, because
    # procedures change and the old one may say the opposite.
    revision: Optional[str] = None
    section: Optional[str] = None

    # The exact text the agent claims supports its statement. Verbatim.
    quoted: str = Field(min_length=12, max_length=400)

    # Character offsets into the source body where `quoted` is claimed to sit.
    start_offset: int = Field(ge=0)
    end_offset: int = Field(ge=0)

    @model_validator(mode="after")
    def offsets_are_ordered(self) -> "Citation":
        if self.end_offset <= self.start_offset:
            raise ValueError("end_offset must exceed start_offset")
        span = self.end_offset - self.start_offset
        # The span must be roughly the length of the quote. A wildly mismatched
        # span means the model produced offsets by guessing, which is the
        # signature we want to catch before the verifier even runs.
        if not (len(self.quoted) * 0.5 <= span <= len(self.quoted) * 2 + 40):
            raise ValueError(
                f"span of {span} chars does not match a quote of "
                f"{len(self.quoted)} chars")
        return self

    @model_validator(mode="after")
    def procedure_citations_pin_a_revision(self) -> "Citation":
        if self.kind == "procedure" and not self.revision:
            raise ValueError("a procedure citation must name the revision")
        return self


class GroundingResult(BaseModel):
    """The outcome of checking a citation against the source it names."""

    citation_index: int
    grounded: bool
    reason: str
    # How close the quote was to the source text at those offsets. 1.0 is
    # exact. Kept as a number rather than a bool because near-misses (a
    # normalized quote, a trimmed ellipsis) are common and are a different
    # problem from a fabricated quote.
    similarity: float = 0.0
