CREATE TABLE IF NOT EXISTS swarm_schema (version integer PRIMARY KEY CHECK(version=1));
INSERT INTO swarm_schema VALUES (1) ON CONFLICT DO NOTHING;
CREATE TABLE IF NOT EXISTS swarm_inbox (
 tenant text NOT NULL, run_id text NOT NULL, attempt integer NOT NULL CHECK(attempt>0),
 job jsonb NOT NULL, job_checksum text NOT NULL, lease_ref text NOT NULL,
 state text NOT NULL DEFAULT 'queued' CHECK(state IN ('queued','running','cancelling','succeeded','failed','expired','cancelled','cancelled_with_orphan_check_pending')),
 accepted_at timestamptz NOT NULL DEFAULT clock_timestamp(), deadline timestamptz NOT NULL,
 owner text, fence bigint NOT NULL DEFAULT 0, lease_until timestamptz,
 deliveries integer NOT NULL DEFAULT 0, cancel_requested boolean NOT NULL DEFAULT false,
 reserved_tokens bigint NOT NULL DEFAULT 0, reserved_cost numeric NOT NULL DEFAULT 0, error_code text,
 PRIMARY KEY(tenant,run_id,attempt)
);
CREATE INDEX IF NOT EXISTS swarm_claimable ON swarm_inbox(accepted_at,lease_until) WHERE state IN ('queued','running','cancelling');
CREATE TABLE IF NOT EXISTS swarm_steps (
 tenant text NOT NULL, run_id text NOT NULL, attempt integer NOT NULL, step_id text NOT NULL, step_key text NOT NULL,
 manifest jsonb, remote_pending boolean NOT NULL DEFAULT false, PRIMARY KEY(tenant,run_id,attempt,step_id),
 FOREIGN KEY(tenant,run_id,attempt) REFERENCES swarm_inbox ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS swarm_events (
 delivery_sequence bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 tenant text NOT NULL, run_id text NOT NULL, attempt integer NOT NULL, step_id text NOT NULL,
 step_sequence integer NOT NULL, event_id text NOT NULL, receipt jsonb NOT NULL,
 UNIQUE(tenant,run_id,attempt,step_id,step_sequence), UNIQUE(tenant,run_id,attempt,event_id),
 FOREIGN KEY(tenant,run_id,attempt) REFERENCES swarm_inbox ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS swarm_event_poll ON swarm_events(tenant,run_id,attempt,delivery_sequence);
CREATE TABLE IF NOT EXISTS swarm_nonces(key_id text NOT NULL,nonce text NOT NULL,expires_at timestamptz NOT NULL,PRIMARY KEY(key_id,nonce));
CREATE INDEX IF NOT EXISTS swarm_nonce_expiry ON swarm_nonces(expires_at);
CREATE TABLE IF NOT EXISTS swarm_rate(key_id text NOT NULL,minute timestamptz NOT NULL,count integer NOT NULL,PRIMARY KEY(key_id,minute));
CREATE TABLE IF NOT EXISTS swarm_conflicts(id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,tenant text NOT NULL,run_id text NOT NULL,attempt integer NOT NULL,checksum text NOT NULL,created_at timestamptz NOT NULL DEFAULT clock_timestamp());
CREATE TABLE IF NOT EXISTS swarm_dlq(tenant text NOT NULL,run_id text NOT NULL,attempt integer NOT NULL,error_code text NOT NULL,created_at timestamptz NOT NULL DEFAULT clock_timestamp(),PRIMARY KEY(tenant,run_id,attempt));
