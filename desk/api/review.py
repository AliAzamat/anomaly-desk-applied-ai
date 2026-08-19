"""The operator-facing API.

Design constraint that shaped everything here: an operator under time pressure
will do the fastest thing available. If recording WHY they overrode the agent
takes an extra click, the note field will be empty within a week.

So the note is required and the category is a set of buttons, and the accept
path has no extra work at all. The friction is placed on the path where the
information exists.
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from desk.routing import executor
from desk.schemas.resolution import ProposedAction
from desk.store import db

router = APIRouter(prefix="/api/desk")

OVERRIDE_CATEGORIES = {
    "wrong_class", "wrong_severity", "wrong_disposition",
    "missing_context", "bad_citation", "agent_correct_but_stale", "other",
}


class OverrideRequest(BaseModel):
    operator_id: str
    category: str
    # Required, with a minimum length that is short enough not to be
    # obstructive and long enough to exclude "n/a" and "wrong". Twelve
    # characters is roughly four words.
    note: str = Field(min_length=12, max_length=1000)
    new_anomaly_class: str | None = None
    new_severity: int | None = Field(default=None, ge=0, le=4)
    new_disposition: str | None = None


class AcceptRequest(BaseModel):
    operator_id: str
    # Optional, and deliberately so. Accepting is the common case and adding
    # required fields to it would slow the queue down for no signal — an
    # accepted triage's label is already known, it is the agent's own output.
    note: str | None = Field(default=None, max_length=1000)


@router.get("/queue")
def queue(limit: int = 50):
    """The review queue, ordered the way an operator wants to work it."""
    with db.conn() as c:
        rows = c.execute(
            """
            SELECT t.task_id, t.station, t.event_ts, t.trigger_reason,
                   d.anomaly_class, d.severity, d.disposition, d.summary,
                   d.agent_confidence, d.calibrated_confidence,
                   d.grounding_rate, d.route, d.routing_reasons, d.payload
            FROM triage_task t
            JOIN triage_decision d ON d.task_id = t.task_id
            WHERE t.status IN ('decided', 'escalated')
              AND d.reviewed_by_human = FALSE
              AND d.route IN ('review', 'escalate')
            ORDER BY d.severity DESC, t.event_ts ASC
            LIMIT %s
            """,
            (limit,),
        ).fetchall()
    return {"items": [dict(r) for r in rows]}


@router.get("/task/{task_id}/evidence")
def evidence(task_id: str):
    """The citations, each with the surrounding source text.

    Returned with CONTEXT around the quote, not just the quote. An operator
    checking a citation needs to see what came before and after it, because the
    most common citation failure that survives automated verification is a
    verbatim quote lifted out of a context that reverses its meaning.
    """
    with db.conn() as c:
        row = c.execute(
            "SELECT payload FROM triage_decision WHERE task_id = %s",
            (task_id,),
        ).fetchone()
    if row is None:
        raise HTTPException(404, "no decision for that task")

    out = []
    for citation in row["payload"].get("citations", []):
        body = _source_body(citation)
        if body is None:
            out.append({**citation, "context": None,
                        "warning": "source not found"})
            continue
        lo = max(citation["start_offset"] - 400, 0)
        hi = min(citation["end_offset"] + 400, len(body))
        out.append({
            **citation,
            "context_before": body[lo:citation["start_offset"]],
            "context_after": body[citation["end_offset"]:hi],
        })
    return {"citations": out}


def _source_body(citation: dict) -> str | None:
    with db.conn() as c:
        if citation["kind"] == "procedure":
            row = c.execute(
                "SELECT body FROM procedure_doc WHERE doc_id = %s AND revision = %s",
                (citation["source_id"], citation.get("revision")),
            ).fetchone()
        else:
            row = c.execute(
                """SELECT narrative || E'\n\nResolution: ' || resolution AS body
                   FROM incident WHERE incident_id = %s""",
                (citation["source_id"],),
            ).fetchone()
    return row["body"] if row else None


@router.post("/task/{task_id}/accept")
def accept(task_id: str, request: AcceptRequest):
    """Operator agrees with the agent. Apply the proposed actions."""
    with db.conn() as c:
        row = c.execute(
            "SELECT payload FROM triage_decision WHERE task_id = %s",
            (task_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(404, "no decision for that task")
        c.execute(
            """UPDATE triage_decision SET reviewed_by_human = TRUE
               WHERE task_id = %s""",
            (task_id,),
        )

    applied = []
    for raw in row["payload"].get("actions", []):
        action = ProposedAction.model_validate(raw)
        created = executor.apply(task_id, action,
                                 applied_by=request.operator_id)
        applied.append({"kind": action.kind, "target": action.target,
                        "created": created})

    db.set_status(task_id, "decided")
    return {"task_id": task_id, "applied": applied}


@router.post("/task/{task_id}/override")
def override(task_id: str, request: OverrideRequest):
    """Operator disagrees. Record exactly what changed and why."""
    if request.category not in OVERRIDE_CATEGORIES:
        raise HTTPException(400, f"unknown category {request.category!r}")

    changed = any(v is not None for v in (
        request.new_anomaly_class, request.new_severity,
        request.new_disposition))
    if not changed and request.category != "agent_correct_but_stale":
        # An override that changes nothing is either a misclick or a
        # disagreement the operator did not express. Rejecting it keeps the
        # override table honest: every row means the agent's output differed
        # from what a human decided.
        raise HTTPException(
            400, "an override must change at least one field, or use the "
                 "'agent_correct_but_stale' category")

    with db.conn() as c:
        c.execute(
            """
            INSERT INTO operator_override
                (override_id, task_id, operator_id, new_anomaly_class,
                 new_severity, new_disposition, note, category)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (uuid.uuid4().hex, task_id, request.operator_id,
             request.new_anomaly_class, request.new_severity,
             request.new_disposition, request.note, request.category),
        )
        c.execute(
            """UPDATE triage_decision SET reviewed_by_human = TRUE
               WHERE task_id = %s""",
            (task_id,),
        )
    db.set_status(task_id, "decided")
    return {"task_id": task_id, "recorded": True}
