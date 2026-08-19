"""Postgres access. Plain SQL, explicit connections, no ORM.

An ORM would be fine here. It is omitted because every query in this project is
one a reader should be able to see whole, and because the judge harness reads
these tables from a different process where a shared session would be a lie.
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Any, Iterator

import psycopg
from psycopg.rows import dict_row

DSN = os.getenv("DESK_DSN", "postgresql://desk:desk@localhost:5432/desk")


@contextmanager
def conn() -> Iterator[psycopg.Connection]:
    with psycopg.connect(DSN, row_factory=dict_row) as c:
        yield c


def insert_triage_task(*, task_id: str, dedupe_key: str, station: str,
                       event_ts: int, trigger_reason: str,
                       severity_floor: int, raw_event: str) -> bool:
    """Insert, ignoring a duplicate. Returns True when a row was created.

    ON CONFLICT DO NOTHING rather than a SELECT-then-INSERT: two consumer
    instances racing on the same redelivered event would both see no row and
    both insert. The unique constraint is the only thing that actually
    serializes them.
    """
    with conn() as c:
        cur = c.execute(
            """
            INSERT INTO triage_task
                (task_id, dedupe_key, station, event_ts, trigger_reason,
                 severity_floor, raw_event)
            VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb)
            ON CONFLICT (dedupe_key) DO NOTHING
            """,
            (task_id, dedupe_key, station, event_ts, trigger_reason,
             severity_floor, raw_event),
        )
        return cur.rowcount == 1


def claim_pending(limit: int = 1) -> list[dict[str, Any]]:
    """Claim pending tasks for this worker.

    SKIP LOCKED is what allows N workers to drain one queue without a
    coordinator. Each worker takes rows nobody else has locked; nobody blocks,
    nobody double-processes.
    """
    with conn() as c:
        rows = c.execute(
            """
            UPDATE triage_task SET status = 'running'
            WHERE task_id IN (
                SELECT task_id FROM triage_task
                WHERE status = 'pending'
                ORDER BY severity_floor DESC, event_ts ASC
                FOR UPDATE SKIP LOCKED
                LIMIT %s
            )
            RETURNING *
            """,
            (limit,),
        ).fetchall()
        return rows


def set_status(task_id: str, status: str) -> None:
    with conn() as c:
        c.execute("UPDATE triage_task SET status = %s WHERE task_id = %s",
                  (status, task_id))
