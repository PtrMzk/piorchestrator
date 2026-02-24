"""SQLite connection management with WAL mode."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from po.db.models import CREATE_TABLES_SQL


def get_connection(db_path: Path) -> sqlite3.Connection:
    """Create or open a SQLite database with WAL mode.

    The database file and parent directories are created if they don't exist.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(db_path: Path) -> sqlite3.Connection:
    """Create and initialize the database with the schema."""
    conn = get_connection(db_path)
    conn.executescript(CREATE_TABLES_SQL)
    return conn
