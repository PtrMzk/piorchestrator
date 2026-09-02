"""SQLite connection management with WAL mode."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from po.db.models import CREATE_TABLES_SQL


def get_connection(db_path: Path) -> sqlite3.Connection:
    """Create or open a SQLite database with WAL mode.

    The database file and parent directories are created if they don't exist.
    Sets restrictive permissions (0o600) on the database file.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    # Restrict DB file to owner-only access
    db_path.chmod(0o600)
    return conn


def init_db(db_path: Path) -> sqlite3.Connection:
    """Create and initialize the database with the schema."""
    conn = get_connection(db_path)
    conn.executescript(CREATE_TABLES_SQL)
    _migrate(conn)
    return conn


# Columns added after the initial schema, per table. CREATE TABLE IF NOT EXISTS
# is a no-op on an existing database, so every column added later needs an entry
# here or older .po/po.db files break on read.
_MIGRATIONS: dict[str, list[tuple[str, str]]] = {
    "tasks": [
        ("input_tokens", "INTEGER"),
        ("output_tokens", "INTEGER"),
        ("num_turns", "INTEGER"),
    ],
    "project": [
        ("setup", "TEXT DEFAULT ''"),
    ],
}


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns that may be missing in older databases."""
    for table, migrations in _MIGRATIONS.items():
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        for col, col_type in migrations:
            if col not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_type}")
    conn.commit()
