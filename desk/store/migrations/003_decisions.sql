-- What the system decided, what the operator did about it, and the join
-- between them. This is the most important table in the project: it is the
-- only place the two scoreboards can be compared.

CREATE TABLE IF NOT EXISTS triage_decision (
    task_id            TEXT PRIMARY KEY REFERENCES triage_task(task_id),
    decided_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    anomaly_class      TEXT NOT NULL,
    severity           SMALLINT NOT NULL,
    root_station       TEXT NOT NULL,
    disposition        TEXT NOT NULL,
    summary            TEXT NOT NULL,
    agent_confidence   REAL NOT NULL,
    calibrated_confidence REAL NOT NULL,
    grounding_rate     REAL NOT NULL,
    route              TEXT NOT NULL CHECK (route IN
                          ('auto_apply', 'review', 'escalate')),
    routing_reasons    TEXT[] NOT NULL,
    -- The full agent output, kept verbatim. Storing the structured columns AND
    -- the raw payload is deliberate duplication: the columns are for queries,
    -- the payload is for the day someone asks a question the columns cannot
    -- answer about a triage from four months ago.
    payload            JSONB NOT NULL,
    trace_id           TEXT NOT NULL,
    model_version      TEXT NOT NULL,
    prompt_version     TEXT NOT NULL,
    -- Whether a human ever looked. Only reviewed triages can contribute to
    -- calibration, because an unreviewed triage has no ground truth.
    reviewed_by_human  BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE TABLE IF NOT EXISTS operator_override (
    override_id     TEXT PRIMARY KEY,
    task_id         TEXT NOT NULL REFERENCES triage_decision(task_id),
    operator_id     TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Exactly what changed. Separate nullable columns rather than a diff blob,
    -- because "how often is the class right but the severity wrong" is a
    -- question we will ask constantly and it should be one query.
    new_anomaly_class TEXT,
    new_severity      SMALLINT,
    new_disposition   TEXT,
    -- Why. Free text from the operator, and the single highest-value field in
    -- the entire schema for improving the system.
    note              TEXT NOT NULL,
    -- A taxonomy the operator picks from, because free text alone cannot be
    -- aggregated and a note field with no category becomes a write-only log.
    category          TEXT NOT NULL CHECK (category IN
                        ('wrong_class', 'wrong_severity', 'wrong_disposition',
                         'missing_context', 'bad_citation', 'agent_correct_but_stale',
                         'other'))
);

CREATE INDEX IF NOT EXISTS idx_override_task ON operator_override (task_id);
CREATE INDEX IF NOT EXISTS idx_override_category
    ON operator_override (category, created_at DESC);

-- Actions that were actually applied, with the idempotency key that makes a
-- retry safe. The executor writes here; the agent never does.
CREATE TABLE IF NOT EXISTS applied_action (
    action_id       TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    task_id         TEXT NOT NULL REFERENCES triage_task(task_id),
    kind            TEXT NOT NULL,
    target          TEXT NOT NULL,
    reason          TEXT NOT NULL,
    applied_by      TEXT NOT NULL,     -- operator id, or 'system:auto_apply'
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    released_at     TIMESTAMPTZ
);
