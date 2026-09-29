"""性能测试：全区规模（5 万学生 x 6 里程碑）一个评估日的全量评估。

非功能要求：5 分钟内跑完。本机实测应远低于此，留足余量断言 240 秒。
"""

import sqlite3
import time

import pytest

from escalation import EscalationService

N_STUDENTS = 50_000
N_PER_STUDENT = 6
EVAL_DATE = "2026-02-15"


@pytest.fixture(scope="module")
def perf_db(tmp_path_factory):
    db = str(tmp_path_factory.mktemp("perf") / "perf.db")
    svc = EscalationService(db, admins=[f"admin{i}" for i in range(5)])
    svc.add_students((f"s{i}", f"c{i % 500}") for i in range(N_STUDENTS))
    svc.add_milestones(
        (
            f"m{i}_{k}",
            f"s{i}",
            f"里程碑{k}",
            f"2026-{(i + k) % 2 + 1:02d}-{(i * 7 + k * 3) % 28 + 1:02d}",
        )
        for i in range(N_STUDENTS)
        for k in range(N_PER_STUDENT)
    )
    svc.conn.close()
    return db


def test_full_evaluation_within_time_budget(perf_db):
    svc = EscalationService(perf_db)
    start = time.perf_counter()
    svc.run_evaluation(EVAL_DATE)
    elapsed = time.perf_counter() - start
    n_overdue = svc.conn.execute(
        "SELECT COUNT(*) FROM milestones WHERE status='overdue'"
    ).fetchone()[0]
    n_notif = svc.conn.execute("SELECT COUNT(*) FROM notifications").fetchone()[0]
    print(
        f"\n全量评估 {N_STUDENTS * N_PER_STUDENT} 个里程碑耗时 {elapsed:.1f}s，"
        f"落后 {n_overdue}，通知 {n_notif}"
    )
    assert elapsed < 240, f"全量评估 {elapsed:.1f}s，超出 5 分钟预算"
