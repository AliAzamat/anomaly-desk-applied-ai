"""Apply approved actions, exactly once.

Separated from the drafter by a module boundary and from the router by a
function call, which together mean there is no code path from a model response
to a side effect that does not pass through this file.
"""
from __future__ import annotations

import hashlib
import logging
import uuid

from desk.schemas.resolution import ProposedAction
from desk.store import db

log = logging.getLogger("desk.executor")


def idempotency_key(task_id: str, action: ProposedAction) -> str:
    """Deterministic key for one action on one task.

    Derived from the content rather than generated, so a retry after a timeout
    produces the SAME key and the unique constraint rejects the duplicate. A
    random uuid here would make every retry a second hold on the same station.
    """
    material = f"{task_id}|{action.kind}|{action.target}"
    return hashlib.sha256(material.encode()).hexdigest()[:32]


def apply(task_id: str, action: ProposedAction, applied_by: str) -> bool:
    """Apply one action. Returns True when this call created it.

    Returns False rather than raising on a duplicate, because a duplicate is
    the expected outcome of a retry and the caller's correct reaction is to
    carry on.
    """
    key = idempotency_key(task_id, action)
    with db.conn() as c:
        cursor = c.execute(
            """
            INSERT INTO applied_action
                (action_id, idempotency_key, task_id, kind, target,
                 reason, applied_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (idempotency_key) DO NOTHING
            """,
            (uuid.uuid4().hex, key, task_id, action.kind, action.target,
             action.reason, applied_by),
        )
        created = cursor.rowcount == 1

    if created:
        log.info("applied %s on %s for task %s by %s",
                 action.kind, action.target, task_id, applied_by)
    else:
        log.info("action %s on %s already applied for task %s (idempotent)",
                 action.kind, action.target, task_id)
    return created
