"""SQLite 连接与建表。"""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS counselors(
    id TEXT PRIMARY KEY
);
CREATE TABLE IF NOT EXISTS admins(
    id TEXT PRIMARY KEY
);
CREATE TABLE IF NOT EXISTS students(
    id TEXT PRIMARY KEY,
    counselor_id TEXT NOT NULL REFERENCES counselors(id)
);
CREATE TABLE IF NOT EXISTS milestones(
    id TEXT PRIMARY KEY,
    student_id TEXT NOT NULL REFERENCES students(id),
    kind TEXT NOT NULL,
    due_date TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending','completed','overdue'))
);
CREATE INDEX IF NOT EXISTS idx_milestones_student ON milestones(student_id);
CREATE INDEX IF NOT EXISTS idx_milestones_status ON milestones(status, due_date);
"""
SCHEMA += """
CREATE TABLE IF NOT EXISTS evaluations(
    eval_date TEXT PRIMARY KEY,
    status TEXT NOT NULL CHECK(status IN ('evaluating','summarizing','completed')),
    cursor TEXT NOT NULL DEFAULT '',
    summary_scope TEXT NOT NULL DEFAULT 'counselor',
    summary_cursor TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS escalations(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    milestone_id TEXT NOT NULL REFERENCES milestones(id),
    eval_date TEXT NOT NULL,
    UNIQUE(milestone_id, eval_date)
);
"""
SCHEMA += """
CREATE TABLE IF NOT EXISTS notifications(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    recipient_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('escalation','counselor_summary','admin_summary')),
    eval_date TEXT NOT NULL,
    escalation_id INTEGER REFERENCES escalations(id),
    body TEXT NOT NULL,
    read INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_notif_escalation_recipient
    ON notifications(escalation_id, recipient_id) WHERE escalation_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_notif_summary
    ON notifications(kind, eval_date, recipient_id)
    WHERE kind IN ('counselor_summary','admin_summary');
"""

BUSY_TIMEOUT_MS = 5000


def connect(db_path: str | Path) -> sqlite3.Connection:
    """打开（必要时创建）数据库连接，设置 WAL 与 busy_timeout。"""
    conn = sqlite3.connect(str(db_path), timeout=BUSY_TIMEOUT_MS / 1000)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    """建表（幂等）。"""
    conn.executescript(SCHEMA)
    conn.commit()
