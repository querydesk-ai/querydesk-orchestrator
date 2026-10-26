-- The phone number behind each WhatsApp user id ("wa-" + HMAC of the number,
-- services/messaging/whatsapp.py). The pipeline, the ticketing service, the
-- logs and decision_logs only ever see the id; this is the one place the
-- number is kept, so an SR status change can be sent back to the right chat.
CREATE TABLE whatsapp_users (
    user_id       text        PRIMARY KEY,
    -- As Twilio sends it, e.g. whatsapp:+447700900123.
    phone         text        NOT NULL,
    created_at    timestamptz NOT NULL DEFAULT now(),
    -- The user's latest inbound message. Outside the Sandbox's 24-hour window
    -- after it, Twilio drops a status update (error 63016).
    last_seen_at  timestamptz NOT NULL DEFAULT now()
);
