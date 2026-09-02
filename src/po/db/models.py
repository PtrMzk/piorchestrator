"""Database schema DDL."""

from __future__ import annotations

CREATE_TABLES_SQL = """
CREATE TABLE IF NOT EXISTS project (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    project_name TEXT NOT NULL,
    description TEXT DEFAULT '',
    default_model TEXT DEFAULT 'sonnet',
    max_concurrency INTEGER DEFAULT 5,
    global_context TEXT DEFAULT '',
    global_context_files TEXT DEFAULT '[]',
    setup TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    description TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK(status IN ('pending','running','completed','failed','cancelled','decomposed')),
    dependencies TEXT NOT NULL DEFAULT '[]',
    context_files TEXT NOT NULL DEFAULT '[]',
    output_files TEXT NOT NULL DEFAULT '[]',
    verification TEXT DEFAULT '',
    priority INTEGER DEFAULT 0,
    model TEXT DEFAULT 'sonnet',
    max_budget_usd REAL DEFAULT 2.0,
    tags TEXT NOT NULL DEFAULT '[]',
    worktree_path TEXT,
    branch_name TEXT,
    session_id TEXT,
    agent_result TEXT,
    error_message TEXT,
    cost_usd REAL,
    duration_ms INTEGER,
    input_tokens INTEGER,
    output_tokens INTEGER,
    num_turns INTEGER,
    attempt INTEGER DEFAULT 0,
    parent_task_id TEXT,
    source TEXT DEFAULT 'spec' CHECK(source IN ('spec','runtime')),
    created_at TEXT DEFAULT (datetime('now')),
    started_at TEXT,
    completed_at TEXT
);
"""
