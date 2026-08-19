-- The triage task: one anomaly, one unit of work, one row.
--
-- Everything the agent layer produces hangs off this row. It exists before any
-- model runs, which means a model failure leaves a task in a known state rather
-- than losing the anomaly entirely.
CREATE TABLE IF NOT EXISTS triage_task (
    task_id         TEXT PRIMARY KEY,
    -- The dedupe key, not the primary key, because a redelivered event should
    -- be rejected but the rejection should be cheap and obvious in the logs.
    dedupe_key      TEXT NOT NULL UNIQUE,
    station         TEXT NOT NULL,
    event_ts        BIGINT NOT NULL,        -- event time, epoch ms, from the machine
    ingested_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    trigger_reason  TEXT NOT NULL,          -- why admission let this through
    severity_floor  SMALLINT NOT NULL,      -- from the fault table, pre-model
    raw_event       JSONB NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'running', 'decided',
                                      'escalated', 'failed'))
);

-- The console shows the open desk sorted by severity then age, which is exactly
-- this index. Without it the operator view does a seq scan on every render and
-- the WebSocket push falls behind under load.
CREATE INDEX IF NOT EXISTS idx_triage_open
    ON triage_task (severity_floor DESC, event_ts ASC)
    WHERE status IN ('pending', 'running');

CREATE INDEX IF NOT EXISTS idx_triage_station_time
    ON triage_task (station, event_ts DESC);
