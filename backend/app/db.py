"""SQLite access (stdlib only). One short-lived connection per call; WAL for concurrent reads."""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS llm_cache (
    cache_key   TEXT PRIMARY KEY,          -- "<prompt>:<version>:<sha256(model+input)>"
    prompt      TEXT NOT NULL,
    version     TEXT NOT NULL,
    model       TEXT NOT NULL,
    response    TEXT NOT NULL,
    in_tokens   INTEGER NOT NULL DEFAULT 0,
    out_tokens  INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS llm_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL DEFAULT (datetime('now')),
    prompt      TEXT NOT NULL,
    version     TEXT NOT NULL,
    tier        TEXT NOT NULL,
    model       TEXT NOT NULL,
    cached      INTEGER NOT NULL,          -- 1 = served from cache (cost 0)
    in_tokens   INTEGER NOT NULL DEFAULT 0,
    out_tokens  INTEGER NOT NULL DEFAULT 0,
    cost_usd    REAL NOT NULL DEFAULT 0,
    latency_ms  INTEGER NOT NULL DEFAULT 0,
    error       TEXT
);

-- Domain tables. symbol = NSE ticker (e.g. RELIANCE) everywhere.
CREATE TABLE IF NOT EXISTS prices (
    symbol TEXT NOT NULL, date TEXT NOT NULL,
    open REAL, high REAL, low REAL, close REAL, volume INTEGER,
    avg_vol_20 REAL,                       -- mean volume of the 20 sessions BEFORE this date (baseline for "2x volume")
    PRIMARY KEY (symbol, date)
);

CREATE TABLE IF NOT EXISTS announcements (
    id TEXT PRIMARY KEY,                   -- "bse:<NEWSID>" / "nse:<seq_id>" / sha1 fallback
    symbol TEXT NOT NULL, ts TEXT NOT NULL,   -- ts: ISO-8601 with +05:30 offset
    subject TEXT, url TEXT,
    category_raw TEXT,                     -- exchange's own label, e.g. "Result / Financial Results"
    pdf_url TEXT,
    detail TEXT,                           -- exchange's longer headline/body text, if any
    source TEXT,                           -- bse | nse
    category TEXT,                         -- OUR classification, see app/classify.py (never overwritten by ingest)
    priority INTEGER,
    summary TEXT
);
CREATE INDEX IF NOT EXISTS ix_ann_symbol_ts ON announcements(symbol, ts);

CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def connect(path: Path) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


# CREATE TABLE IF NOT EXISTS never alters an existing table, so DBs created by the first skeleton
# need these columns added in place.
_ADDED_COLUMNS = {
    "prices": {"open": "REAL", "high": "REAL", "low": "REAL", "avg_vol_20": "REAL"},
    "announcements": {"category_raw": "TEXT", "pdf_url": "TEXT", "detail": "TEXT", "source": "TEXT"},
}


def init_db(path: Path) -> None:
    conn = connect(path)
    try:
        conn.executescript(SCHEMA)
        for table, cols in _ADDED_COLUMNS.items():
            have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
            for col, typ in cols.items():
                if col not in have:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
        conn.commit()
    finally:
        conn.close()
