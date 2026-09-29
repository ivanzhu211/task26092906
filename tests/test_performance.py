"""全量评估性能：5 万学生 × 6 里程碑，一个评估日 < 5 分钟。"""
import time
from datetime import date, timedelta

from escalation import run_evaluation
from escalation.db import connect, init_schema

N_STUDENTS = 50_000
M_PER = 6
N_ADMINS = 3
EVAL_DATE = "2026-02-15"


def _bulk_seed(db: str) -> None:
    conn = connect(db)
    init_schema(conn)
    with conn:
        conn.executemany(
            "INSERT INTO admins(id) VALUES (?)",
            [(f"admin{i}",) for i in range(N_ADMINS)],
        )
        n_counselors = 200
        conn.executemany(
            "INSERT INTO counselors(id) VALUES (?)",
            [(f"coun{i}",) for i in range(n_counselors)],
        )
        conn.executemany(
            "INSERT INTO students(id, counselor_id) VALUES (?, ?)",
            [(f"stu{i}", f"coun{i % n_counselors}") for i in range(N_STUDENTS)],
        )
        base = date(2026, 1, 1)
        rows = []
        for i in range(N_STUDENTS):
            for m in range(M_PER):
                due = base + timedelta(days=(i * M_PER + m) % 60)
                rows.append((f"ms{i}_{m}", f"stu{i}", f"kind{m}", due.isoformat()))
        conn.executemany(
            "INSERT INTO milestones(id, student_id, kind, due_date, status)"
            " VALUES (?, ?, ?, ?, 'pending')",
            rows,
        )
    conn.close()


def test_full_evaluation_under_5_minutes(tmp_path):
    db = str(tmp_path / "perf.db")
    _bulk_seed(db)
    start = time.monotonic()
    run_evaluation(db, EVAL_DATE)
    elapsed = time.monotonic() - start
    assert elapsed < 300, f"全量评估超时: {elapsed:.1f}s"

    conn = connect(db)
    n_esc = conn.execute("SELECT COUNT(*) c FROM escalations").fetchone()["c"]
    n_notif = conn.execute("SELECT COUNT(*) c FROM notifications").fetchone()["c"]
    n_summaries = conn.execute(
        "SELECT COUNT(*) c FROM notifications"
        " WHERE kind IN ('counselor_summary','admin_summary')"
    ).fetchone()["c"]
    conn.close()
    assert n_esc > 0
    assert n_summaries == 200 + N_ADMINS  # 每位辅导员+每位管理员各一条
    print(f"\n全量评估 {N_STUDENTS * M_PER} 里程碑: {elapsed:.1f}s, "
          f"升级 {n_esc}, 通知 {n_notif}")
