-- Empty means legacy provenance is unknown. Never infer it from an ID prefix.
ALTER TABLE events ADD COLUMN identity_key TEXT NOT NULL DEFAULT '';
