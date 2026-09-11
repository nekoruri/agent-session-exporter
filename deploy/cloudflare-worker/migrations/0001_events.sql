CREATE TABLE events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint TEXT NOT NULL UNIQUE,
    source TEXT NOT NULL CHECK (source = 'claude-cloud'),
    device_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    event_name TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    cwd TEXT NOT NULL,
    project TEXT NOT NULL,
    repository TEXT NOT NULL,
    branch TEXT NOT NULL,
    transcript_path TEXT NOT NULL CHECK (transcript_path = ''),
    payload_json TEXT NOT NULL,
    received_at TEXT NOT NULL
) STRICT;

CREATE INDEX events_session_idx
ON events(source, device_id, session_id, id);
