-- 005: real quota sync + per-customer provider rights. Additive only.
ALTER TABLE provider_connections ADD COLUMN quota_remaining INTEGER;
ALTER TABLE provider_connections ADD COLUMN quota_synced_at TEXT;
ALTER TABLE api_keys ADD COLUMN allowed_providers TEXT;
