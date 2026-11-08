-- SPDX-License-Identifier: GPL-3.0-or-later
-- The schema of SyncMusic 0.2.0: schema_version 1. It folds the 13 development migrations into one
-- file; a later schema change is a numbered file after it (002_*.sql), see syncmusic/infra/db.py.

CREATE TABLE libraries (
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL,
    name TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    config TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE devices (
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL,
    name TEXT NOT NULL,
    config TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE templates (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    data TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE snapshots (
    library_id TEXT PRIMARY KEY,
    state_token TEXT NOT NULL,
    taken_at TEXT NOT NULL,
    data BLOB NOT NULL
);

CREATE TABLE jobs (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    device_id TEXT,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    finished_at TEXT,
    data TEXT NOT NULL
);

CREATE TABLE sync_reports (
    id TEXT PRIMARY KEY,
    device_id TEXT NOT NULL,
    job_id TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    data TEXT NOT NULL
);

CREATE INDEX sync_reports_device ON sync_reports(device_id, started_at);

CREATE TABLE submitted_plays (
    sink TEXT NOT NULL,
    library_id TEXT NOT NULL,
    item_id TEXT NOT NULL,
    played_at TEXT NOT NULL,
    device_id TEXT NOT NULL,
    submitted_at TEXT NOT NULL,
    PRIMARY KEY (sink, library_id, item_id, played_at, device_id)
);

CREATE TABLE pending_feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id TEXT NOT NULL,
    kind TEXT NOT NULL,          -- play | rating | position
    data TEXT NOT NULL,
    created_at TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT
, inflight INTEGER, tried_at TEXT, tries INTEGER NOT NULL DEFAULT 0);

CREATE TABLE local_feedback (     -- reverse data for libraries that are not FeedbackSinks (Folder)
    library_id TEXT NOT NULL,
    item_id TEXT NOT NULL,
    play_count INTEGER NOT NULL DEFAULT 0,
    last_played TEXT,
    rating INTEGER,
    position_s REAL,
    PRIMARY KEY (library_id, item_id)
);

CREATE TABLE cache_entries (
    key TEXT PRIMARY KEY,
    path TEXT NOT NULL,
    size INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    last_access TEXT NOT NULL
, meta TEXT NOT NULL DEFAULT '{}');

CREATE INDEX cache_entries_access ON cache_entries(last_access);

CREATE TABLE settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE feedback_journal (
    device_id TEXT PRIMARY KEY,
    play_counts TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE processor_templates (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    data TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE deleted_default_templates (
    id TEXT PRIMARY KEY,
    library_id TEXT NOT NULL
);

CREATE TABLE notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    event TEXT NOT NULL,
    level TEXT NOT NULL,
    device_id TEXT,
    device_name TEXT,
    data TEXT NOT NULL,
    link TEXT,
    read INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX notifications_read ON notifications (read);

CREATE TABLE position_scopes (
    device_id TEXT NOT NULL,
    scope TEXT NOT NULL,
    position_s REAL NOT NULL,
    PRIMARY KEY (device_id, scope)
);

CREATE TABLE device_items (
    device_id TEXT PRIMARY KEY,
    keys TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE dropped_feedback (       -- given up after MAX_ATTEMPTS permanent rejections
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id TEXT NOT NULL,
    kind TEXT NOT NULL,               -- play | rating | position
    data TEXT NOT NULL,
    attempts INTEGER NOT NULL,
    error TEXT NOT NULL,
    dropped_at TEXT NOT NULL
);

CREATE INDEX dropped_feedback_device ON dropped_feedback (device_id, id);

CREATE TABLE device_health (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id TEXT NOT NULL,
    at TEXT NOT NULL,
    kind TEXT NOT NULL,          -- 'write' | 'io_error' | 'ack'
    bytes INTEGER NOT NULL DEFAULT 0,
    seconds REAL NOT NULL DEFAULT 0,
    detail TEXT NOT NULL DEFAULT ''
);

CREATE INDEX device_health_device ON device_health (device_id, at);

CREATE TABLE replaygain (
    item_key TEXT PRIMARY KEY,   -- ItemRef.key(): library id + item id
    source_state TEXT NOT NULL,
    gain_db REAL NOT NULL,
    peak REAL NOT NULL,
    analysed_at TEXT NOT NULL
);
