"""对外服务：学生/里程碑注册、辅导员操作、通知查询。

夜间评估的实现放在 nightly.py，这里只做委托。
"""

import datetime
from contextlib import contextmanager

from .db import connect


class EvaluationDateError(Exception):
    """评估日早于已完成评估日。"""


class InvalidOperationError(Exception):
    """非法的状态操作。"""


def _iso(value):
    """接受 datetime.date 或 ISO 字符串，统一为 ISO 字符串。"""
    if isinstance(value, datetime.datetime):
        return value.date().isoformat()
    if isinstance(value, datetime.date):
        return value.isoformat()
    # 校验格式，避免脏数据进入按字典序比较的日期列
    return datetime.date.fromisoformat(str(value)).isoformat()


class EscalationService:
    def __init__(self, db_path, admins=()):
        self.db_path = str(db_path)
        self.conn = connect(self.db_path)
        with self._tx():
            self.conn.executemany(
                "INSERT OR IGNORE INTO admins(id) VALUES (?)",
                [(a,) for a in admins],
            )

    @contextmanager
    def _tx(self):
        """短写事务：BEGIN IMMEDIATE 立即拿写锁，配合 busy_timeout 等待。"""
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        else:
            self.conn.execute("COMMIT")

    # ---- 数据注册（上层在夜间任务之外建好） ----

    def add_students(self, students):
        """students: 可迭代的 (student_id, counselor_id)。"""
        rows = [(sid, cid) for sid, cid in students]
        with self._tx():
            self.conn.executemany(
                "INSERT OR IGNORE INTO counselors(id) VALUES (?)",
                [(cid,) for _, cid in rows],
            )
            self.conn.executemany(
                "INSERT INTO students(id, counselor_id) VALUES (?, ?)", rows
            )

    def add_milestones(self, milestones):
        """milestones: 可迭代的 (milestone_id, student_id, name, due_date)。"""
        rows = [(mid, sid, name, _iso(due)) for mid, sid, name, due in milestones]
        with self._tx():
            self.conn.executemany(
                "INSERT INTO milestones(id, student_id, name, due_date, status)"
                " VALUES (?, ?, ?, ?, 'pending')",
                rows,
            )

    # ---- 辅导员操作（要求 1 秒内完成：均为单行短事务） ----

    def mark_completed(self, milestone_id):
        """标记完成：未完成/落后 -> 已完成；已完成则幂等。"""
        with self._tx():
            cur = self.conn.execute(
                "UPDATE milestones SET status='completed'"
                " WHERE id=? AND status IN ('pending','overdue')",
                (milestone_id,),
            )
            if cur.rowcount == 0:
                self._require_milestone(milestone_id)

    def undo_completed(self, milestone_id):
        """撤销完成：已完成 -> 未完成。其他状态报错。"""
        with self._tx():
            cur = self.conn.execute(
                "UPDATE milestones SET status='pending'"
                " WHERE id=? AND status='completed'",
                (milestone_id,),
            )
            if cur.rowcount == 0:
                self._require_milestone(milestone_id)
                raise InvalidOperationError("只有已完成的里程碑才能撤销完成")

    def change_due_date(self, milestone_id, new_due_date):
        """修改截止日期；落后的里程碑回到未完成，其余状态不变。"""
        with self._tx():
            cur = self.conn.execute(
                "UPDATE milestones SET due_date=?,"
                " status = CASE WHEN status='overdue' THEN 'pending' ELSE status END"
                " WHERE id=?",
                (_iso(new_due_date), milestone_id),
            )
            if cur.rowcount == 0:
                self._require_milestone(milestone_id)

    def _require_milestone(self, milestone_id):
        row = self.conn.execute(
            "SELECT 1 FROM milestones WHERE id=?", (milestone_id,)
        ).fetchone()
        if row is None:
            raise InvalidOperationError(f"里程碑不存在: {milestone_id}")

    # ---- 通知 ----

    def list_notifications(self, recipient_id):
        """按发出顺序返回该接收人的全部通知（含已读状态）。"""
        return [
            dict(row)
            for row in self.conn.execute(
                "SELECT id, recipient_id, kind, eval_date, escalation_id, body, read"
                " FROM notifications WHERE recipient_id=? ORDER BY id",
                (recipient_id,),
            )
        ]

    def mark_read(self, notification_id, recipient_id):
        """接收人把自己的一条通知标为已读。"""
        with self._tx():
            self.conn.execute(
                "UPDATE notifications SET read=1 WHERE id=? AND recipient_id=?",
                (notification_id, recipient_id),
            )

    # ---- 查询辅助 ----

    def milestone_status(self, milestone_id):
        row = self.conn.execute(
            "SELECT status FROM milestones WHERE id=?", (milestone_id,)
        ).fetchone()
        if row is None:
            raise InvalidOperationError(f"里程碑不存在: {milestone_id}")
        return row["status"]

    # ---- 夜间评估 ----

    def run_evaluation(self, eval_date, crash_hook=None):
        """跑一个评估日；有未完成的上一个评估日时先补完它。

        crash_hook: 测试注入点，到达检查点时回调（见 nightly.env_crash_hook）。
        """
        from .nightly import run_evaluation

        return run_evaluation(self.conn, self._tx, _iso(eval_date), crash_hook)
