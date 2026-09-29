"""测试辅助：确定性建数、整库快照对比。"""
from __future__ import annotations

import sqlite3
from datetime import date, timedelta

from escalation.db import connect
from escalation.service import EscalationService

DUE_BASE = date(2026, 1, 1)


def seed_service(db_path: str, n_counselors: int = 3, students_per: int = 20,
                 milestones_per: int = 6, n_admins: int = 2) -> EscalationService:
    """建一批学生与里程碑，due_date 错开以便产生不同的逾期时间。"""
    svc = EscalationService(db_path)
    svc.initialize([f"admin{i}" for i in range(n_admins)])
    for c in range(n_counselors):
        svc.add_counselor(f"coun{c}")
    for c in range(n_counselors):
        for s in range(students_per):
            sid = f"stu{c}_{s}"
            svc.add_student(sid, f"coun{c}")
            for m in range(milestones_per):
                mid = f"ms{c}_{s}_{m}"
                due = DUE_BASE + timedelta(days=(s * milestones_per + m) % 40)
                svc.add_milestone(mid, sid, f"kind{m}", due.isoformat())
    return svc


def snapshot(db_path: str) -> dict[str, list[tuple]]:
    """导出全部业务表内容（有序），用于逐条对比两个数据库。"""
    conn = connect(db_path)
    try:
        out = {}
        for table, order in [
            ("milestones", "id"),
            ("evaluations", "eval_date"),
            ("escalations", "id"),
            ("notifications", "id"),
        ]:
            rows = conn.execute(
                f"SELECT * FROM {table} ORDER BY {order}"
            ).fetchall()
            out[table] = [tuple(r) for r in rows]
        return out
    finally:
        conn.close()


def assert_same_data(db_a: str, db_b: str) -> None:
    a, b = snapshot(db_a), snapshot(db_b)
    for table in a:
        assert a[table] == b[table], (
            f"表 {table} 不一致:\n{a[table][:5]}\nvs\n{b[table][:5]}"
            f"\n(len {len(a[table])} vs {len(b[table])})"
        )
