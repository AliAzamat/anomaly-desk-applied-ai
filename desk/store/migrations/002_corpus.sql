-- Two corpora, deliberately in two tables with different shapes, because they
-- answer different questions and a single "documents" table would erase the
-- difference.
--
-- procedure_chunk answers "what does the written process say to do".
-- incident answers "what actually happened last time and what fixed it".

CREATE TABLE IF NOT EXISTS procedure_doc (
    doc_id       TEXT PRIMARY KEY,          -- e.g. 'SPX-MP-4412'
    title        TEXT NOT NULL,
    revision     TEXT NOT NULL,             -- procedures are revision-controlled
    effective_at TIMESTAMPTZ NOT NULL,
    superseded_at TIMESTAMPTZ,              -- NULL means currently in force
    body         TEXT NOT NULL
);

-- Chunks carry their character offsets into the parent body. This is the field
-- that makes citation verification possible: a citation names a doc, a section,
-- and a span, and we can go read exactly those characters back out.
CREATE TABLE IF NOT EXISTS procedure_chunk (
    chunk_id     TEXT PRIMARY KEY,
    doc_id       TEXT NOT NULL REFERENCES procedure_doc(doc_id),
    section      TEXT NOT NULL,             -- e.g. '5.3.2'
    start_offset INT NOT NULL,
    end_offset   INT NOT NULL,
    text         TEXT NOT NULL,
    embedding    VECTOR(1536),
    -- Generated tsvector for the lexical half of hybrid search. Generated
    -- rather than maintained by a trigger so it cannot drift from `text`.
    tsv          TSVECTOR GENERATED ALWAYS AS (to_tsvector('english', text)) STORED
);

CREATE INDEX IF NOT EXISTS idx_chunk_tsv ON procedure_chunk USING GIN (tsv);
CREATE INDEX IF NOT EXISTS idx_chunk_vec ON procedure_chunk
    USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);

CREATE TABLE IF NOT EXISTS incident (
    incident_id   TEXT PRIMARY KEY,
    station       TEXT NOT NULL,
    fault_code    TEXT,
    occurred_at   TIMESTAMPTZ NOT NULL,
    -- What the operator wrote. Free text, authored by a human, which makes it
    -- the highest-value corpus and also the injection surface. Step 8.
    narrative     TEXT NOT NULL,
    resolution    TEXT NOT NULL,
    -- Did the fix hold? An incident whose resolution failed is a NEGATIVE
    -- example and retrieving it as if it were a good answer is worse than
    -- retrieving nothing.
    resolution_held BOOLEAN NOT NULL DEFAULT TRUE,
    embedding     VECTOR(1536),
    tsv           TSVECTOR GENERATED ALWAYS AS (
                      to_tsvector('english', narrative || ' ' || resolution)
                  ) STORED
);

CREATE INDEX IF NOT EXISTS idx_incident_tsv ON incident USING GIN (tsv);
CREATE INDEX IF NOT EXISTS idx_incident_station ON incident (station, occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_incident_vec ON incident
    USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);
