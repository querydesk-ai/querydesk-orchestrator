-- One row per status change a user is told about (PRD F9, Day 7).
-- `status` is the orchestrator's addition to the PRD's columns: the ticketing
-- state machine never revisits a status, so (sr_id, status) is unique and a
-- retried webhook, a replay or a reconciliation overlap writes nothing new.
CREATE TABLE notifications (
    id          bigserial   PRIMARY KEY,
    user_id     text        NOT NULL,
    -- The ticketing service's SR id (sr_requests.ticket_id).
    sr_id       text        NOT NULL,
    status      text        NOT NULL,
    message     text        NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    read_at     timestamptz,
    UNIQUE (sr_id, status)
);

-- Day 8's GET /notifications: a user's latest first.
CREATE INDEX notifications_user_created_idx ON notifications (user_id, created_at DESC);

-- The webhook and the reconciliation poll look up the raised SR by ticket id.
CREATE INDEX sr_requests_ticket_id_idx ON sr_requests (ticket_id);
