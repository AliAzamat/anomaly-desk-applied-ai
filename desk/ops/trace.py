"""Spans, one per agent hop.

A trace here has to answer one question well: an operator says a triage from
last Tuesday was wrong, and you need to reconstruct exactly what the system saw
and did. Not approximately. Exactly.

That requirement is what makes this more than logging. Logs tell you what
happened somewhere. A trace tells you what happened to THIS request, in order,
with the timing and the cost of each part.
"""
from __future__ import annotations

import contextlib
import json
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Iterator

from desk.ops.pricing import cost_usd


@dataclass
class Span:
    name: str
    trace_id: str
    span_id: str
    parent_id: str | None
    started_at: float
    ended_at: float | None = None
    attributes: dict[str, Any] = field(default_factory=dict)
    children: list["Span"] = field(default_factory=list)

    def set(self, key: str, value: Any) -> None:
        self.attributes[key] = value

    def record_model_call(self, model: str, prompt_tokens: int,
                          completion_tokens: int) -> None:
        """Token counts and the derived cost, on the span that made the call.

        Recorded per span rather than summed at the top, because "this triage
        cost 4.2 cents" is nearly useless and "the drafter is 78% of the cost
        and the classifier is 6%" tells you exactly where to work.
        """
        self.set("model", model)
        self.set("prompt_tokens", prompt_tokens)
        self.set("completion_tokens", completion_tokens)
        self.set("cost_usd", cost_usd(model, prompt_tokens, completion_tokens))

    @property
    def duration_ms(self) -> float:
        end = self.ended_at if self.ended_at is not None else time.perf_counter()
        return (end - self.started_at) * 1000.0

    @contextlib.contextmanager
    def child(self, name: str) -> Iterator["Span"]:
        span = Span(name=name, trace_id=self.trace_id,
                    span_id=uuid.uuid4().hex[:16], parent_id=self.span_id,
                    started_at=time.perf_counter())
        self.children.append(span)
        try:
            yield span
        except Exception as exc:
            # The exception is recorded on the span AND re-raised. Swallowing
            # it here would make the tracing layer change program behaviour,
            # which is the one thing an observability layer must never do.
            span.set("error", f"{type(exc).__name__}: {exc}")
            raise
        finally:
            span.ended_at = time.perf_counter()

    def total_cost_usd(self) -> float:
        return (self.attributes.get("cost_usd", 0.0)
                + sum(c.total_cost_usd() for c in self.children))

    def cost_by_stage(self) -> dict[str, float]:
        """Cost attributed to the top-level stage that incurred it."""
        out: dict[str, float] = {}
        for child in self.children:
            out[child.name] = child.total_cost_usd()
        return out


# Fields that must never reach the trace store. Procedure bodies can be export
# controlled and incident narratives can name people, and a trace store is
# typically readable by a much broader group than the source documents are.
#
# This is a SECURITY control, not a privacy courtesy: retrieval enforces who
# can read a procedure, and a trace containing the procedure body would route
# around that enforcement entirely.
REDACT_KEYS = {"procedure_body", "narrative", "quoted", "operator_note",
               "raw_response"}

# Identifiers that look like restricted document ids get masked in free text.
DOC_ID_PATTERN = re.compile(r"\bSPX-[A-Z]{2}-\d{4}\b")


def redact(attributes: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in attributes.items():
        if key in REDACT_KEYS:
            # Length is kept, content is not. Length is often exactly what you
            # need when debugging ("the evidence block was 40 characters, so
            # retrieval returned nothing") and it leaks nothing.
            out[key] = f"<redacted {len(str(value))} chars>"
        elif isinstance(value, str):
            out[key] = DOC_ID_PATTERN.sub("<doc-id>", value)
        else:
            out[key] = value
    return out


def new_trace(name: str, trace_id: str | None = None) -> Span:
    return Span(name=name, trace_id=trace_id or uuid.uuid4().hex,
                span_id=uuid.uuid4().hex[:16], parent_id=None,
                started_at=time.perf_counter())


def serialize(span: Span) -> dict[str, Any]:
    return {
        "name": span.name,
        "trace_id": span.trace_id,
        "span_id": span.span_id,
        "parent_id": span.parent_id,
        "duration_ms": round(span.duration_ms, 2),
        "attributes": redact(span.attributes),
        "children": [serialize(c) for c in span.children],
    }


def persist(span: Span) -> None:
    from desk.store import db
    with db.conn() as c:
        c.execute(
            """
            INSERT INTO trace_span (trace_id, root_name, payload,
                                    total_cost_usd, duration_ms)
            VALUES (%s, %s, %s::jsonb, %s, %s)
            ON CONFLICT (trace_id) DO NOTHING
            """,
            (span.trace_id, span.name, json.dumps(serialize(span)),
             span.total_cost_usd(), span.duration_ms),
        )
