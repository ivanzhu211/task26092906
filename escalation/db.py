"""SQLite 连接与 schema 管理。

设计要点：
- WAL 模式 + busy_timeout：夜间任务与另一进程的辅导员操作共用同一文件时，
  写事务短小、相互等待有上限，保证辅导员操作 1 秒内完成。
- 所有日期以 ISO 格式（YYYY-MM-DD）字符串存储，可直接按字典序比较。
"""

import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS counselors (
    id TEXT PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS admins (
    id TEXT PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS students (
    id           TEXT PRIMARY KEY,
    counselor_id TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS milestones (
    id             TEXT PRIMARY KEY,
    student_id     TEXT NOT NULL REFERENCES students(id),
    name           TEXT NOT NULL,
    due_date       TEXT NOT NULL,
    status         TEXT NOT NULL CHECK (status IN ('pending', 'completed', 'overdue')),
    escalation_seq INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_milestones_student ON milestones(student_id);

-- 一次“进入落后”即一次升级；通知挂在升级上，保证每位接收人恰好一条。
CREATE TABLE IF NOT EXISTS escalations (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    milestone_id TEXT NOT NULL REFERENCES milestones(id),
    seq          INTEGER NOT NULL,
    eval_date    TEXT NOT NULL,
    UNIQUE (milestone_id, seq),
    UNIQUE (milestone_id, eval_date)
);
CREATE INDEX IF NOT EXISTS idx_escalations_date ON escalations(eval_date);

-- 断点续跑：同一评估日内每个里程碑只判定一次。
CREATE TABLE IF NOT EXISTS eval_judgments (
    eval_date    TEXT NOT NULL,
    milestone_id TEXT NOT NULL,
    PRIMARY KEY (eval_date, milestone_id)
);

-- 每个评估日一条运行记录，phase 记录进度，崩溃后从对应阶段继续。
CREATE TABLE IF NOT EXISTS eval_runs (
    eval_date    TEXT PRIMARY KEY,
    phase        TEXT NOT NULL CHECK (phase IN
        ('judging', 'counselor_summaries', 'admin_summaries', 'done')),
    judge_cursor INTEGER NOT NULL DEFAULT 0
);

-- 通知按接收人一行，已读状态各自独立。
CREATE TABLE IF NOT EXISTS notifications (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    recipient_id  TEXT NOT NULL,
    kind          TEXT NOT NULL CHECK (kind IN
        ('escalation', 'counselor_summary', 'admin_summary')),
    eval_date     TEXT,
    escalation_id INTEGER REFERENCES escalations(id),
    body          TEXT NOT NULL,
    read          INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_notif_escalation
    ON notifications(escalation_id, recipient_id)
    WHERE escalation_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_notif_summary
    ON notifications(kind, eval_date, recipient_id)
    WHERE kind IN ('counselor_summary', 'admin_summary');
CREATE INDEX IF NOT EXISTS idx_notif_recipient ON notifications(recipient_id);
"""

# 评估日完成水位线的 meta key
META_LAST_COMPLETED_EVAL_DATE = "last_completed_eval_date"


def connect(db_path):
    """打开（必要时创建）数据库并确保 schema 存在。"""
    conn = sqlite3.connect(db_path, timeout=5.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(SCHEMA)
    return conn
