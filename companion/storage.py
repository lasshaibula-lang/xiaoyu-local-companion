from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def now_iso() -> str:
    return datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="microseconds")


def data_dir() -> Path:
    directory = Path(os.environ.get("COMPANION_DATA_DIR", str(ROOT / "data"))).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    return directory


@contextmanager
def get_connection():
    conn = sqlite3.connect(data_dir() / "companion.db", timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=15000")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    with get_connection() as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS messages (
          id TEXT PRIMARY KEY, role TEXT NOT NULL, content TEXT NOT NULL,
          mode TEXT NOT NULL, created_at TEXT NOT NULL, request_id TEXT
        );
        CREATE INDEX IF NOT EXISTS messages_created ON messages(created_at);
        CREATE TABLE IF NOT EXISTS requests (
          id TEXT PRIMARY KEY, response TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS memories (
          id TEXT PRIMARY KEY, content TEXT NOT NULL, kind TEXT NOT NULL,
          source TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
          pinned INTEGER NOT NULL DEFAULT 0, deleted_at TEXT, embedding TEXT
        );
        CREATE TABLE IF NOT EXISTS tombstones (
          id TEXT PRIMARY KEY, content TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS documents (
          id TEXT PRIMARY KEY, title TEXT NOT NULL, path TEXT UNIQUE NOT NULL,
          content TEXT NOT NULL, content_hash TEXT NOT NULL, updated_at TEXT NOT NULL,
          managed INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS chunks (
          id TEXT PRIMARY KEY, document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
          content TEXT NOT NULL, embedding TEXT, ordinal INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS chunks_document ON chunks(document_id);
        CREATE TABLE IF NOT EXISTS usage (
          id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL, model TEXT NOT NULL,
          purpose TEXT NOT NULL, input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL,
          estimated_cost REAL NOT NULL, estimated INTEGER NOT NULL DEFAULT 1
        );
        """)


def get_state(key: str, default=None):
    with get_connection() as conn:
        row = conn.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
    return json.loads(row["value"]) if row else default


def set_state(key: str, value) -> None:
    with get_connection() as conn:
        conn.execute("INSERT INTO state(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     (key, json.dumps(value, ensure_ascii=False)))
