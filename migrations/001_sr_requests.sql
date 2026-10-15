-- The orchestrator's record of every request it raised or escalated (PRD Day 6).
-- RAG answers are not recorded here: they are not requests.
CREATE TABLE sr_requests (
    id                    bigserial PRIMARY KEY,
    -- The ticketing service's SR id; null for a MANUAL_ESCALATION.
    ticket_id             text,
    user_id               text        NOT NULL,
    -- Null when an escalation was never routed to a category.
    category_id           text,
    details               jsonb       NOT NULL DEFAULT '{}'::jsonb,
    status                text        NOT NULL,
    resolution_type       text        NOT NULL
        CHECK (resolution_type IN ('RAG', 'AUTO_SR', 'MANUAL_ESCALATION')),
    requires_approval     boolean     NOT NULL DEFAULT false,
    retrieval_confidence  double precision,
    -- Day 7: the last status the user was notified about.
    last_notified_status  text,
    created_at            timestamptz NOT NULL DEFAULT now(),
    updated_at            timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX sr_requests_user_category_status_idx
    ON sr_requests (user_id, category_id, status);
