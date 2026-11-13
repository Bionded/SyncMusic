-- Wi-Fi clients: one row per paired client (its device id, the install's uid, the SHA-256 of its token; the
-- previous token stays valid for a while after a rotation) and the short-lived pairing codes
CREATE TABLE client_pairings (
    device_id TEXT PRIMARY KEY,
    client_uid TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    client TEXT NOT NULL,
    capabilities TEXT NOT NULL,
    token_sha256 TEXT,
    old_token_sha256 TEXT,
    old_token_until TEXT,
    created_at TEXT NOT NULL,
    last_seen TEXT,
    revoked_at TEXT
);

CREATE TABLE client_pairing_codes (
    code TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used_at TEXT,
    failures INTEGER NOT NULL DEFAULT 0
);
