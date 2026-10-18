-- One row per chat message the decision layer judged (PRD F10): its inputs,
-- the decision and the latency of each step. The same record is also logged
-- as one querydesk.decision line.
--
-- Beyond the PRD's columns: `turn`, `rule`, `category_id`, `sr_id` and `error`
-- are queried often enough to be columns; `details` holds the rest of the
-- record (intent, overrides, missing_fields, extraction_ok, router_confident,
-- requires_approval). `jev_output` is always null while the rule engine is
-- the default (docs/jev-notes.md).
CREATE TABLE decision_logs (
    id               bigserial   PRIMARY KEY,
    user_id          text        NOT NULL,
    query            text        NOT NULL,
    -- 0 for a fresh message, then 1, 2… for each reply in a conversation.
    turn             integer     NOT NULL,
    -- The top dense cosine similarity (never the fused score, invariant #1).
    top_dense_score  double precision,
    -- [[category_id, score], [category_id, score]]
    router_top_two   jsonb,
    jev_output       jsonb,
    engine           text        NOT NULL,
    decision         text        NOT NULL,
    rule             integer,
    category_id      text,
    -- The ticketing service's SR id, when one was raised or found open.
    sr_id            text,
    -- Set when acting on the decision failed (generation, SR creation).
    error            text,
    latency_ms       jsonb       NOT NULL,
    details          jsonb       NOT NULL,
    -- When the decision was made. The row is written in the background, so
    -- the app sets this rather than the insert's now().
    created_at       timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX decision_logs_user_created_idx ON decision_logs (user_id, created_at DESC);
