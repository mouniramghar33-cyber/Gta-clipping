"""SQLite storage (WAL mode, one connection per thread)."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS streamers (
    id INTEGER PRIMARY KEY,
    login TEXT UNIQUE NOT NULL,
    platform TEXT NOT NULL DEFAULT 'twitch',
    display_name TEXT,
    enabled INTEGER NOT NULL DEFAULT 1,
    rights_granted INTEGER NOT NULL DEFAULT 0,
    notes TEXT DEFAULT '',
    added_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS streamer_channels (
    streamer TEXT NOT NULL,          -- streamers.login (watchlist id)
    platform TEXT NOT NULL,          -- twitch | kick | youtube
    handle TEXT NOT NULL,            -- twitch login, kick slug, youtube channel id
    priority INTEGER NOT NULL DEFAULT 0,
    note TEXT,
    platform_user_id TEXT,           -- e.g. Kick broadcaster_user_id (resolved at runtime)
    PRIMARY KEY (streamer, platform, handle)
);
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY,
    streamer TEXT NOT NULL,
    dir TEXT NOT NULL,
    started_at REAL NOT NULL,
    ended_at REAL,
    title TEXT,
    category TEXT,
    stream_started_at REAL
);
CREATE TABLE IF NOT EXISTS chat_bins (
    streamer TEXT NOT NULL,
    t INTEGER NOT NULL,          -- bin start (unix seconds, multiple of window_sec)
    messages INTEGER NOT NULL,
    users INTEGER NOT NULL,
    hype INTEGER NOT NULL,
    ratio REAL,
    z REAL,
    PRIMARY KEY (streamer, t)
);
CREATE TABLE IF NOT EXISTS moments (
    id INTEGER PRIMARY KEY,
    streamer TEXT NOT NULL,
    session_id INTEGER,
    t_start REAL NOT NULL,       -- detected event start (wall clock, reaction delay removed)
    t_end REAL NOT NULL,
    peak_t REAL NOT NULL,
    peak_ratio REAL,
    peak_z REAL,
    signals TEXT,                -- json
    status TEXT NOT NULL,        -- open|queued|processing|done|failed|skipped
    error TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS moments_status ON moments(status);
CREATE TABLE IF NOT EXISTS clips (
    id INTEGER PRIMARY KEY,
    moment_id INTEGER,
    streamer TEXT NOT NULL,
    path TEXT NOT NULL,
    thumb TEXT,
    duration REAL,
    score REAL,
    scores TEXT,                 -- json component scores
    priority INTEGER DEFAULT 0,
    title TEXT, hook TEXT, description TEXT, hashtags TEXT,
    thumbnail_suggestion TEXT, explanation TEXT,
    transcript TEXT, language TEXT, layout TEXT,
    src_start REAL, src_end REAL, peak_t REAL, stream_offset REAL,
    status TEXT NOT NULL,        -- pending|approved|rejected|auto_rejected|published
    public_token TEXT,
    created_at REAL NOT NULL,
    decided_at REAL
);
CREATE INDEX IF NOT EXISTS clips_status ON clips(status);
CREATE TABLE IF NOT EXISTS secrets (
    name TEXT PRIMARY KEY,
    value_enc TEXT NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS kv (
    k TEXT PRIMARY KEY,
    v TEXT,
    updated_at REAL
);
CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY,
    provider TEXT NOT NULL,          -- youtube | tiktok | buffer | uploadpost | instagram
    platform TEXT NOT NULL,          -- where the video ends up: youtube | tiktok | instagram | ...
    external_id TEXT NOT NULL,
    username TEXT, display_name TEXT, avatar_url TEXT,
    tokens_enc TEXT,                 -- Fernet-encrypted JSON
    token_expires_at REAL, refresh_expires_at REAL,
    status TEXT NOT NULL DEFAULT 'connected',   -- connected | expired | error | disconnected
    status_message TEXT,
    auto_publish INTEGER NOT NULL DEFAULT 0,
    dry_run INTEGER NOT NULL DEFAULT 1,
    daily_limit INTEGER NOT NULL DEFAULT 3,
    min_interval_min INTEGER NOT NULL DEFAULT 120,
    posting_times TEXT,              -- json ["12:00","18:30"] or "auto" / "immediate"
    style TEXT NOT NULL DEFAULT 'clean',
    streamers TEXT,                  -- json list, null = all
    min_score REAL,
    privacy TEXT,
    ai_disclosure INTEGER NOT NULL DEFAULT 0,
    settings TEXT,                   -- json, provider specific
    created_at REAL NOT NULL, updated_at REAL NOT NULL,
    UNIQUE(provider, external_id)
);
CREATE TABLE IF NOT EXISTS publish_jobs (
    id INTEGER PRIMARY KEY,
    clip_id INTEGER NOT NULL,
    account_id INTEGER NOT NULL,
    status TEXT NOT NULL,            -- awaiting_approval|scheduled|uploading|processing|published|failed|cancelled|dry_run|skipped
    scheduled_at REAL,
    title TEXT, description TEXT, hashtags TEXT,
    options TEXT,                    -- json: privacy, interaction settings, disclosures...
    style TEXT,
    adjusted_score REAL,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at REAL,
    lease_until REAL,
    upload_state TEXT,               -- json: resumable session, publish_id ...
    remote_id TEXT, url TEXT, error TEXT,
    public_token TEXT,
    dry_run INTEGER NOT NULL DEFAULT 0,
    approved_at REAL, published_at REAL,
    created_at REAL NOT NULL, updated_at REAL NOT NULL,
    UNIQUE(clip_id, account_id)
);
CREATE INDEX IF NOT EXISTS jobs_status ON publish_jobs(status, scheduled_at);
CREATE TABLE IF NOT EXISTS metrics (
    id INTEGER PRIMARY KEY,
    job_id INTEGER NOT NULL,
    fetched_at REAL NOT NULL,
    age_hours REAL,
    views INTEGER, likes INTEGER, comments INTEGER, shares INTEGER,
    watch_time_min REAL, avg_view_pct REAL, subs_gained INTEGER,
    raw TEXT
);
CREATE INDEX IF NOT EXISTS metrics_job ON metrics(job_id, fetched_at);
CREATE TABLE IF NOT EXISTS clip_variants (
    clip_id INTEGER NOT NULL,
    style TEXT NOT NULL,
    path TEXT NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY (clip_id, style)
);
CREATE TABLE IF NOT EXISTS oauth_states (
    state TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    verifier TEXT,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS publications (
    id INTEGER PRIMARY KEY,
    clip_id INTEGER NOT NULL,
    platform TEXT NOT NULL,
    status TEXT NOT NULL,        -- uploading|done|failed
    remote_id TEXT, url TEXT, error TEXT,
    created_at REAL NOT NULL, updated_at REAL NOT NULL
);
"""

JSON_FIELDS = {"signals", "scores", "hashtags", "options", "upload_state", "streamers", "settings",
               "posting_times", "raw", "spec", "labels", "aka"}

# Columns added after the first release (applied to existing databases on start).
MIGRATIONS = {
    "clips": {"master_dir": "TEXT", "top_cat": "TEXT", "category": "TEXT", "quote": "TEXT",
              "labels": "TEXT", "gta": "INTEGER DEFAULT 0", "collab": "TEXT", "latency": "REAL",
              "tier": "TEXT", "platform": "TEXT"},
    "streamers": {"name": "TEXT", "aka": "TEXT", "source": "TEXT DEFAULT 'manual'", "followers": "INTEGER",
                  "weight": "REAL DEFAULT 1.0"},
    "moments": {"collab": "TEXT", "gta": "INTEGER DEFAULT 0", "platform": "TEXT", "category": "TEXT"},
    "sessions": {"platform": "TEXT", "handle": "TEXT"},
}


class DB:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self._local = threading.local()
        c = self.conn()
        c.executescript(SCHEMA)
        for table, cols in MIGRATIONS.items():
            have = {r[1] for r in c.execute(f"PRAGMA table_info({table})")}
            for col, typ in cols.items():
                if col not in have:
                    c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")

    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "c", None)
        if c is None:
            c = sqlite3.connect(self.path, timeout=30, isolation_level=None)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=NORMAL")
            c.execute("PRAGMA busy_timeout=30000")
            self._local.c = c
        return c

    # ---- helpers -------------------------------------------------------
    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        return self.conn().execute(sql, tuple(params))

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[dict]:
        return [self._row(r) for r in self.conn().execute(sql, tuple(params)).fetchall()]

    def one(self, sql: str, params: Iterable[Any] = ()) -> dict | None:
        r = self.conn().execute(sql, tuple(params)).fetchone()
        return self._row(r) if r else None

    @staticmethod
    def _row(r: sqlite3.Row) -> dict:
        d = dict(r)
        for k in JSON_FIELDS & d.keys():
            if d[k]:
                try:
                    d[k] = json.loads(d[k])
                except ValueError:
                    pass
        return d

    def insert(self, table: str, data: dict) -> int:
        data = {k: (json.dumps(v) if k in JSON_FIELDS and not isinstance(v, str) else v) for k, v in data.items()}
        cols = ",".join(data)
        qs = ",".join("?" * len(data))
        return self.conn().execute(f"INSERT INTO {table} ({cols}) VALUES ({qs})", tuple(data.values())).lastrowid

    def update(self, table: str, row_id: int, data: dict) -> None:
        data = {k: (json.dumps(v) if k in JSON_FIELDS and not isinstance(v, str) else v) for k, v in data.items()}
        sets = ",".join(f"{k}=?" for k in data)
        self.conn().execute(f"UPDATE {table} SET {sets} WHERE id=?", (*data.values(), row_id))

    # ---- key/value -----------------------------------------------------
    def kv_get(self, k: str, default=None):
        r = self.one("SELECT v FROM kv WHERE k=?", (k,))
        return json.loads(r["v"]) if r and r["v"] is not None else default

    def kv_set(self, k: str, v) -> None:
        self.execute("INSERT OR REPLACE INTO kv (k, v, updated_at) VALUES (?,?,?)", (k, json.dumps(v), time.time()))

    # ---- streamers -----------------------------------------------------
    def add_streamer(self, login: str, platform: str = "twitch", rights_granted: bool = False) -> None:
        self.execute(
            "INSERT OR IGNORE INTO streamers (login, platform, rights_granted, added_at) VALUES (?,?,?,?)",
            (login.lower().strip(), platform, int(rights_granted), time.time()),
        )

    def channels(self, streamer: str) -> list[dict]:
        return self.query("SELECT * FROM streamer_channels WHERE streamer=? ORDER BY priority", (streamer,))

    def streamers(self, enabled_only: bool = False) -> list[dict]:
        q = "SELECT * FROM streamers" + (" WHERE enabled=1" if enabled_only else "") + " ORDER BY login"
        return self.query(q)

    # ---- chat bins -----------------------------------------------------
    def upsert_chat_bin(self, streamer: str, t: int, messages: int, users: int, hype: int,
                        ratio: float | None, z: float | None) -> None:
        self.execute(
            "INSERT OR REPLACE INTO chat_bins VALUES (?,?,?,?,?,?,?)",
            (streamer, t, messages, users, hype, ratio, z),
        )

    def chat_bins(self, streamer: str, since: float) -> list[dict]:
        return self.query("SELECT * FROM chat_bins WHERE streamer=? AND t>=? ORDER BY t", (streamer, int(since)))
