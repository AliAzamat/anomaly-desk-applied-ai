"""Tools the drafting agent can call.

Every tool is one of two kinds and the distinction is enforced, not documented:

  READ  — queries state. Safe to call repeatedly, safe to call speculatively,
          safe to call during an eval run against production data.
  WRITE — changes something outside this process. Never executed by the agent.
          The agent emits a ProposedAction; the executor runs it later, under
          a policy, with an idempotency key.

There is no third kind. A tool that "just sends a notification" is a write.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
from dataclasses import dataclass
from typing import Any, Callable

from desk.store import db

log = logging.getLogger("desk.tools")


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]      # JSON Schema, given to the model
    fn: Callable[..., Any]
    is_write: bool


class ToolError(Exception):
    """A tool failed in a way the agent should see and can react to.

    Returned into the conversation as a tool message rather than raised out of
    the loop. A tool failing is normal — a station id that does not exist, a
    query with no rows — and the agent recovering from it is the behaviour we
    want. Crashing the whole triage because one lookup missed is not.
    """


# ---------------------------------------------------------------- read tools

def get_station_history(station: str, hours: int = 24) -> dict[str, Any]:
    """Recent event summary for a station, from the console project's tables."""
    if hours > 168:
        raise ToolError("history window capped at 168 hours")
    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours)
    with db.conn() as c:
        rows = c.execute(
            """
            SELECT trigger_reason, severity_floor, event_ts, status
            FROM triage_task
            WHERE station = %s AND ingested_at >= %s
            ORDER BY event_ts DESC
            LIMIT 50
            """,
            (station, since),
        ).fetchall()
    if not rows:
        # An empty result is a RESULT, not an error. The agent needs to be able
        # to distinguish "this station has been quiet" from "the lookup broke",
        # and returning an error for the quiet case teaches it the wrong thing.
        return {"station": station, "hours": hours, "triages": [],
                "note": "no triages recorded in this window"}
    return {"station": station, "hours": hours,
            "triages": [dict(r) for r in rows]}


def get_open_holds() -> dict[str, Any]:
    """Which stations and lots are currently held.

    The agent needs this to avoid proposing a hold on something already held,
    which is the single most common redundant proposal in this system.
    """
    with db.conn() as c:
        rows = c.execute(
            """
            SELECT target, kind, created_at, reason
            FROM applied_action
            WHERE kind IN ('hold_station', 'quarantine_lot')
              AND released_at IS NULL
            ORDER BY created_at DESC
            """
        ).fetchall()
    return {"open_holds": [dict(r) for r in rows]}


def get_lot_for_station(station: str) -> dict[str, Any]:
    with db.conn() as c:
        row = c.execute(
            """
            SELECT lot_id, unit_count, entered_at
            FROM station_lot
            WHERE station = %s AND exited_at IS NULL
            ORDER BY entered_at DESC LIMIT 1
            """,
            (station,),
        ).fetchone()
    if row is None:
        raise ToolError(f"no active lot at {station}")
    return dict(row)


# --------------------------------------------------------------- write tools
#
# These exist as specs so the model knows their shape and can propose them.
# Their `fn` raises if anyone calls it from the agent path. The executor
# imports the underlying implementation directly, which is the only sanctioned
# route to a side effect.

def _write_tools_are_not_callable(**kwargs) -> Any:
    raise ToolError(
        "write tools are proposed, never executed by the agent; "
        "emit a ProposedAction instead")


REGISTRY: dict[str, ToolSpec] = {}


def register(spec: ToolSpec) -> None:
    REGISTRY[spec.name] = spec


register(ToolSpec(
    name="get_station_history",
    description="Recent triage history for a station. Use to see whether this "
                "anomaly is a recurrence.",
    parameters={
        "type": "object",
        "properties": {
            "station": {"type": "string"},
            "hours": {"type": "integer", "minimum": 1, "maximum": 168},
        },
        "required": ["station"],
    },
    fn=get_station_history,
    is_write=False,
))

register(ToolSpec(
    name="get_open_holds",
    description="Stations and lots currently held. Check before proposing a hold.",
    parameters={"type": "object", "properties": {}},
    fn=get_open_holds,
    is_write=False,
))

register(ToolSpec(
    name="get_lot_for_station",
    description="The lot currently at a station, for quarantine proposals.",
    parameters={
        "type": "object",
        "properties": {"station": {"type": "string"}},
        "required": ["station"],
    },
    fn=get_lot_for_station,
    is_write=False,
))

register(ToolSpec(
    name="hold_station",
    description="PROPOSE holding a station. This does not execute; it is "
                "reviewed and applied by the executor.",
    parameters={
        "type": "object",
        "properties": {
            "station": {"type": "string"},
            "reason": {"type": "string"},
        },
        "required": ["station", "reason"],
    },
    fn=_write_tools_are_not_callable,
    is_write=True,
))


def invoke(name: str, arguments: dict[str, Any]) -> str:
    """Run a tool and return its result as a string for the conversation."""
    spec = REGISTRY.get(name)
    if spec is None:
        return json.dumps({"error": f"unknown tool {name!r}"})
    if spec.is_write:
        # Defense in depth. The loop below also refuses to call write tools;
        # this check exists so that a future caller who forgets cannot cause a
        # side effect by accident.
        return json.dumps({
            "error": "this tool is proposal-only",
            "instruction": "include it in `actions` in your final answer",
        })
    try:
        return json.dumps(spec.fn(**arguments), default=str)
    except ToolError as exc:
        return json.dumps({"error": str(exc)})
    except TypeError as exc:
        # Wrong arguments from the model. Returned rather than raised so the
        # agent can correct itself on the next turn.
        return json.dumps({"error": f"bad arguments: {exc}"})
