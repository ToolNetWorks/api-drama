-- 004: four-state connection verification.
-- key_valid: NULL = never checked, 1 = gateway accepted the key, 0 = rejected (401/403).
-- endpoint_ok: NULL = no verified endpoint checked yet, 1 = configured path returned 2xx,
--   0 = configured path failed (wrong path or upstream error).
-- last_check_ok keeps its meaning: last *endpoint* check passed (2xx on verified path).
-- Runs once (tracked in schema_migrations). Additive only.
ALTER TABLE provider_connections ADD COLUMN key_valid INTEGER;
ALTER TABLE provider_connections ADD COLUMN endpoint_ok INTEGER;
