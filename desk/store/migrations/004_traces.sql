CREATE TABLE IF NOT EXISTS trace_span (
    trace_id        TEXT PRIMARY KEY,
    root_name       TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    payload         JSONB NOT NULL,
    total_cost_usd  NUMERIC(10, 6) NOT NULL,
    duration_ms     REAL NOT NULL
);

-- Traces are queried by time window almost exclusively, and they are the
-- highest-volume table here by a wide margin.
CREATE INDEX IF NOT EXISTS idx_trace_created ON trace_span (created_at DESC);

-- Retention. Traces are big and their value decays fast: the debugging window
-- for "why was this triage wrong" is weeks, not years. The decision row keeps
-- the trace_id forever, so an old triage still has an identifier even after
-- its trace has aged out — you lose the detail, not the thread.
CREATE INDEX IF NOT EXISTS idx_trace_cost
    ON trace_span (created_at DESC, total_cost_usd DESC);
