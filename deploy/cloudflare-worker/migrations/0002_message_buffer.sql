CREATE TABLE message_chunks (
    stream TEXT NOT NULL,
    idx INTEGER NOT NULL CHECK (idx >= 0 AND idx < 4096),
    final INTEGER NOT NULL CHECK (final IN (0, 1)),
    key_id TEXT NOT NULL,
    nonce TEXT NOT NULL,
    ciphertext TEXT NOT NULL,
    PRIMARY KEY (stream, idx)
) STRICT;

-- Receipts contain no message text and keep late retries from recreating staging data.
CREATE TABLE message_receipts (
    stream TEXT PRIMARY KEY,
    event_id INTEGER NOT NULL REFERENCES events(id)
) STRICT;
