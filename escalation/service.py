"""上层平台调用的库入口：初始化、辅导员日常操作、通知查询。"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .db import connect, init_schema

PENDING = "pending"
COMPLETED = "completed"
OVERDUE = "overdue"


@dataclass(frozen=True)
class Notification:
    id: int
    recipient_id: str
    kind: str  # 'escalation' | 'counselor_summary' | 'admin_summary'
    eval_date: str
    body: str
    read: bool


class EscalationService:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)
        conn = connect(self.db_path)
        try:
            init_schema(conn)
        finally:
            conn.close()

    def _conn(self) -> sqlite3.Connection:
        return connect(self.db_path)

    def initialize(self, admin_ids: list[str]) -> None:
        """初始化管理员名单（仅首次，重复调用忽略已存在的）。"""
        conn = self._conn()
        try:
            with conn:
                conn.executemany(
                    "INSERT OR IGNORE INTO admins(id) VALUES (?)",
                    [(a,) for a in admin_ids],
                )
        finally:
            conn.close()

    # ---- 上层建数 ----
    def add_counselor(self, counselor_id: str) -> None:
        conn = self._conn()
        try:
            with conn:
                conn.execute(
                    "INSERT OR IGNORE INTO counselors(id) VALUES (?)", (counselor_id,)
                )
        finally:
            conn.close()

    def add_student(self, student_id: str, counselor_id: str) -> None:
        conn = self._conn()
        try:
            with conn:
                conn.execute(
                    "INSERT OR IGNORE INTO counselors(id) VALUES (?)", (counselor_id,)
                )
                conn.execute(
                    "INSERT INTO students(id, counselor_id) VALUES (?, ?)",
                    (student_id, counselor_id),
                )
        finally:
            conn.close()

    def add_milestone(
        self, milestone_id: str, student_id: str, kind: str, due_date: str
    ) -> None:
        conn = self._conn()
        try:
            with conn:
                conn.execute(
                    "INSERT INTO milestones(id, student_id, kind, due_date, status)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (milestone_id, student_id, kind, due_date, PENDING),
                )
        finally:
            conn.close()

    # ---- 辅导员日常操作（均不会直接变为落后） ----
    def mark_complete(self, milestone_id: str) -> None:
        conn = self._conn()
        try:
            with conn:
                cur = conn.execute(
                    "UPDATE milestones SET status=? WHERE id=?",
                    (COMPLETED, milestone_id),
                )
                if cur.rowcount == 0:
                    raise KeyError(f"未知里程碑: {milestone_id}")
        finally:
            conn.close()

    def unmark_complete(self, milestone_id: str) -> None:
        conn = self._conn()
        try:
            with conn:
                cur = conn.execute(
                    "UPDATE milestones SET status=? WHERE id=? AND status=?",
                    (PENDING, milestone_id, COMPLETED),
                )
                if cur.rowcount == 0:
                    raise KeyError(f"里程碑不是已完成状态: {milestone_id}")
        finally:
            conn.close()

    def change_due_date(self, milestone_id: str, new_due_date: str) -> None:
        """修改截止日期；落后的里程碑回到未完成，其余状态不变。"""
        conn = self._conn()
        try:
            with conn:
                cur = conn.execute(
                    "UPDATE milestones SET due_date=?,"
                    " status=CASE WHEN status=? THEN ? ELSE status END"
                    " WHERE id=?",
                    (new_due_date, OVERDUE, PENDING, milestone_id),
                )
                if cur.rowcount == 0:
                    raise KeyError(f"未知里程碑: {milestone_id}")
        finally:
            conn.close()

    # ---- 通知 ----
    def get_notifications(self, recipient_id: str) -> list[Notification]:
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT id, recipient_id, kind, eval_date, body, read"
                " FROM notifications WHERE recipient_id=? ORDER BY id",
                (recipient_id,),
            ).fetchall()
            return [
                Notification(
                    id=r["id"],
                    recipient_id=r["recipient_id"],
                    kind=r["kind"],
                    eval_date=r["eval_date"],
                    body=r["body"],
                    read=bool(r["read"]),
                )
                for r in rows
            ]
        finally:
            conn.close()

    def mark_notification_read(self, notification_id: int, recipient_id: str) -> None:
        conn = self._conn()
        try:
            with conn:
                cur = conn.execute(
                    "UPDATE notifications SET read=1 WHERE id=? AND recipient_id=?",
                    (notification_id, recipient_id),
                )
                if cur.rowcount == 0:
                    raise KeyError(f"通知不存在或不属于该接收人: {notification_id}")
        finally:
            conn.close()

    # ---- 状态查询（测试/上层用） ----
    def milestone_status(self, milestone_id: str) -> str:
        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT status FROM milestones WHERE id=?", (milestone_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"未知里程碑: {milestone_id}")
            return row["status"]
        finally:
            conn.close()
