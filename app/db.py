# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Kevin Villarreal (Homelab Club)
"""SQLite storage. Small, single-file, lives in the /data volume."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from typing import Any

from . import crypto

DB_PATH = os.path.join(crypto.DATA_DIR, "patchrack.db")
_lock = threading.RLock()
_conn: sqlite3.Connection | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT UNIQUE NOT NULL,
    pw_hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS servers (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    host TEXT NOT NULL,
    port INTEGER NOT NULL DEFAULT 22,
    username TEXT NOT NULL,
    key_source TEXT NOT NULL DEFAULT 'app',      -- 'app' (shared app key) | 'custom'
    private_key_enc TEXT,
    passphrase_enc TEXT,
    become TEXT NOT NULL DEFAULT 'sudo',         -- 'sudo' | 'sudo_password' | 'root'
    sudo_password_enc TEXT,
    upgrade_mode TEXT NOT NULL DEFAULT 'full',   -- 'full' (dist-upgrade) | 'safe' (upgrade)
    autoremove INTEGER NOT NULL DEFAULT 1,
    reboot_policy TEXT NOT NULL DEFAULT 'ask',   -- default pre-selected in the update dialog
    notes TEXT,
    host_key TEXT,
    host_key_fp TEXT,
    status TEXT NOT NULL DEFAULT 'unknown',      -- unknown|up_to_date|updates|reboot|error|unreachable
    pending_count INTEGER NOT NULL DEFAULT 0,
    security_count INTEGER NOT NULL DEFAULT 0,
    kept_back_count INTEGER NOT NULL DEFAULT 0,
    reboot_required INTEGER NOT NULL DEFAULT 0,
    packages_json TEXT,
    os_name TEXT,
    kernel TEXT,
    uptime_s INTEGER,
    last_error TEXT,
    last_check_at REAL,
    last_update_at REAL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS history (
    id INTEGER PRIMARY KEY,
    job_id TEXT,
    server_id INTEGER,
    server_name TEXT,
    kind TEXT NOT NULL,          -- check | update
    trigger TEXT,                -- manual | schedule
    result TEXT NOT NULL,        -- success | failed
    summary TEXT,
    log TEXT,
    started_at REAL NOT NULL,
    finished_at REAL
);
CREATE INDEX IF NOT EXISTS idx_history_started ON history(started_at DESC);
"""

DEFAULT_SETTINGS: dict[str, Any] = {
    "schedule_enabled": True,
    "check_interval_hours": 6,
    "max_parallel": 3,
    "reboot_timeout_min": 10,
    "notify_updates_found": True,
    "notify_update_done": True,
    "notify_update_failed": True,
    "notify_unreachable": True,
    "notify_container_updates": True,
    "pushover_enabled": False,
    "pushover_user": "",
    "prowl_enabled": False,
    "app_url": "",
    "last_scheduled_check": 0,
}
# settings stored encrypted
SECRET_SETTINGS = {"pushover_token", "pushover_user", "prowl_apikey", "app_private_key"}


def conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        os.umask(0o077)  # database, WAL and key files readable by the app user only
        os.makedirs(crypto.DATA_DIR, exist_ok=True)
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False, isolation_level=None)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA foreign_keys=ON")
        _conn.executescript(SCHEMA)
        _migrate(_conn)
        try:
            os.chmod(DB_PATH, 0o600)
        except OSError:
            pass
    return _conn


MIGRATIONS = {
    "docker_status": "TEXT NOT NULL DEFAULT 'unknown'",   # unknown|none|ok|no_access|error
    "docker_json": "TEXT",
    "docker_updates": "INTEGER NOT NULL DEFAULT 0",
    "docker_error": "TEXT",
}


def _migrate(c: sqlite3.Connection) -> None:
    have = {r[1] for r in c.execute("PRAGMA table_info(servers)")}
    for col, decl in MIGRATIONS.items():
        if col not in have:
            c.execute(f"ALTER TABLE servers ADD COLUMN {col} {decl}")


def q(sql: str, args: tuple = ()) -> list[sqlite3.Row]:
    with _lock:
        return conn().execute(sql, args).fetchall()


def q1(sql: str, args: tuple = ()) -> sqlite3.Row | None:
    with _lock:
        return conn().execute(sql, args).fetchone()


def ex(sql: str, args: tuple = ()) -> int:
    with _lock:
        cur = conn().execute(sql, args)
        return cur.lastrowid


# ---- settings ---------------------------------------------------------------
def get_setting(key: str, default: Any = None) -> Any:
    row = q1("SELECT value FROM settings WHERE key=?", (key,))
    if row is None or row["value"] is None:
        return DEFAULT_SETTINGS.get(key, default)
    if key in SECRET_SETTINGS:
        return crypto.decrypt(row["value"], f"setting:{key}")
    return json.loads(row["value"])


def set_setting(key: str, value: Any) -> None:
    if key in SECRET_SETTINGS:
        stored = crypto.encrypt(value, f"setting:{key}") if value else None
    else:
        stored = json.dumps(value)
    ex("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
       (key, stored))


def has_setting(key: str) -> bool:
    row = q1("SELECT value FROM settings WHERE key=?", (key,))
    return bool(row and row["value"])


def verify_master_key() -> None:
    """Store an encrypted canary on first run; refuse to start if the key changed."""
    row = q1("SELECT value FROM settings WHERE key='canary'")
    if row is None:
        ex("INSERT INTO settings(key,value) VALUES('canary',?)", (crypto.encrypt("patchrack", "canary"),))
        return
    try:
        crypto.decrypt(row["value"], "canary")
    except crypto.DecryptError:
        raise SystemExit("The master secret does not match this database. Restore the original "
                         "PATCHRACK_SECRET, or remove the database to start over.") from None


def now() -> float:
    return time.time()
