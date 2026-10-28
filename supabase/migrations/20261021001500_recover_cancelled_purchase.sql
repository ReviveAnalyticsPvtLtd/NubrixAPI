-- Additive recovery readiness: the payment recovery worker heartbeat.
SET LOCAL lock_timeout='5s';
CREATE TABLE IF NOT EXISTS private.payment_recovery_runtime (
 singleton boolean PRIMARY KEY DEFAULT true CHECK(singleton),
 worker_seen_at timestamptz NOT NULL
);
ALTER TABLE private.payment_recovery_runtime ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON private.payment_recovery_runtime FROM PUBLIC,anon,authenticated,service_role;
GRANT SELECT,INSERT,UPDATE ON private.payment_recovery_runtime TO service_role;
