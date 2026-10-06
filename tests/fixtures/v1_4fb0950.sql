-- Exact v1 SCHEMA from main commit 4fb0950, kept independent of new migrations.

CREATE TABLE IF NOT EXISTS chats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'chat',
    created REAL NOT NULL,
    updated REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS messages_chat ON messages(chat_id, id);
CREATE TABLE IF NOT EXISTS facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    text TEXT NOT NULL UNIQUE,
    created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    cron TEXT NOT NULL,
    prompt TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    approved INTEGER NOT NULL DEFAULT 1,
    origin TEXT NOT NULL DEFAULT 'panel',
    next_run REAL NOT NULL,
    created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS job_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL,
    job_name TEXT NOT NULL,
    started REAL NOT NULL,
    finished REAL,
    ok INTEGER,
    output TEXT
);
CREATE INDEX IF NOT EXISTS job_runs_started ON job_runs(started);
CREATE TABLE IF NOT EXISTS usage (
    day TEXT PRIMARY KEY,
    calls INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    created REAL NOT NULL,
    expires REAL NOT NULL,
    last_seen REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
