"""并发测试：夜间任务运行时，另一个进程持续做辅导员操作。

结束后校验「任何时候」不变式：
- 没有里程碑既是已完成又是落后；
- 每个处于落后的里程碑，对每位接收人恰好有一条对应当前升级的通知；
- 每条汇总都和当天实际发出的升级通知对得上。
"""

import os
import re
import sqlite3
import subprocess
import sys

import pytest

import escalation.nightly as nightly
from escalation import EscalationService

OPS_WORKER = os.path.join(os.path.dirname(__file__), "ops_worker.py")

N_STUDENTS = 6000
N_PER_STUDENT = 6
N_MILESTONES = N_STUDENTS * N_PER_STUDENT
N_COUNSELORS = 50
ADMINS = ["admin1", "admin2", "admin3"]
EVAL_DATE = "2026-02-15"


def build_dataset(db_path):
    svc = EscalationService(db_path, admins=ADMINS)
    svc.add_students((f"s{i}", f"c{i % N_COUNSELORS}") for i in range(N_STUDENTS))
    svc.add_milestones(
        (
            f"m{i * N_PER_STUDENT + k}",
            f"s{i}",
            f"里程碑{k}",
            f"2026-{(i + k) % 2 + 1:02d}-{(i * 7 + k * 3) % 28 + 1:02d}",
        )
        for i in range(N_STUDENTS)
        for k in range(N_PER_STUDENT)
    )
    svc.conn.close()


@pytest.fixture
def db_with_concurrent_ops(tmp_path, monkeypatch):
    db = str(tmp_path / "concurrent.db")
    sentinel = str(tmp_path / "stop")
    build_dataset(db)
    worker = subprocess.Popen(
        [sys.executable, OPS_WORKER, db, sentinel, str(N_MILESTONES)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert worker.stdout.readline().strip() == "READY"
        monkeypatch.setattr(nightly, "JUDGE_BATCH_SIZE", 500)
        svc = EscalationService(db)
        svc.run_evaluation(EVAL_DATE)
        svc.conn.close()
    finally:
        open(sentinel, "w").close()
        out, err = worker.communicate(timeout=60)
    assert worker.returncode == 0, err
    max_op_ms = float(re.search(r"MAX_OP_MS=([\d.]+)", out).group(1))
    ops = int(re.search(r"OPS=(\d+)", out).group(1))
    assert ops > 20, f"并发操作太少（{ops} 次），测试没意义"
    assert max_op_ms < 1000, f"辅导员操作超过 1 秒: {max_op_ms}ms"
    return db


def test_no_milestone_is_both_completed_and_overdue(db_with_concurrent_ops):
    """状态是单值列，完成与落后结构性互斥；这里校验并发操作后无非法残留。"""
    conn = sqlite3.connect(db_with_concurrent_ops)
    bad = conn.execute(
        "SELECT COUNT(*) FROM milestones"
        " WHERE status NOT IN ('pending', 'completed', 'overdue')"
    ).fetchone()[0]
    assert bad == 0
    # 已完成的里程碑不应有待判定的落后状态：完成优先于一切
    completed = conn.execute(
        "SELECT COUNT(*) FROM milestones WHERE status = 'completed'"
    ).fetchone()[0]
    assert completed > 0, "并发操作应产生过完成标记"


def test_every_overdue_milestone_has_exactly_one_notification_per_recipient(
    db_with_concurrent_ops,
):
    conn = sqlite3.connect(db_with_concurrent_ops)
    admins = [r[0] for r in conn.execute("SELECT id FROM admins")]
    overdue = conn.execute(
        "SELECT m.id, m.escalation_seq, s.counselor_id"
        " FROM milestones m JOIN students s ON s.id = m.student_id"
        " WHERE m.status = 'overdue'"
    ).fetchall()
    assert overdue, "数据集中应有落后里程碑"
    for milestone_id, seq, counselor_id in overdue:
        esc = conn.execute(
            "SELECT id FROM escalations WHERE milestone_id=? AND seq=?",
            (milestone_id, seq),
        ).fetchall()
        assert len(esc) == 1, f"{milestone_id} 第 {seq} 次升级记录不唯一"
        recipients = sorted(
            r[0]
            for r in conn.execute(
                "SELECT recipient_id FROM notifications WHERE escalation_id=?",
                (esc[0][0],),
            )
        )
        assert recipients == sorted([counselor_id, *admins]), (
            f"{milestone_id} 当前升级的通知接收人不齐"
        )


def test_every_escalation_notifies_exactly_counselor_plus_admins(
    db_with_concurrent_ops,
):
    conn = sqlite3.connect(db_with_concurrent_ops)
    n_admins = conn.execute("SELECT COUNT(*) FROM admins").fetchone()[0]
    rows = conn.execute(
        "SELECT e.id, COUNT(n.id) FROM escalations e"
        " LEFT JOIN notifications n ON n.escalation_id = e.id"
        " GROUP BY e.id HAVING COUNT(n.id) != ?",
        (1 + n_admins,),
    ).fetchall()
    assert rows == []


def test_counselor_summaries_match_escalations(db_with_concurrent_ops):
    conn = sqlite3.connect(db_with_concurrent_ops)
    summaries = conn.execute(
        "SELECT recipient_id, eval_date, body FROM notifications"
        " WHERE kind = 'counselor_summary'"
    ).fetchall()
    assert summaries
    for counselor_id, eval_date, body in summaries:
        actual = conn.execute(
            "SELECT m.student_id, m.name, e.seq"
            " FROM escalations e"
            " JOIN milestones m ON m.id = e.milestone_id"
            " JOIN students s ON s.id = m.student_id"
            " WHERE e.eval_date = ? AND s.counselor_id = ?"
            " ORDER BY e.id",
            (eval_date, counselor_id),
        ).fetchall()
        header = f"【汇总】评估日 {eval_date}：名下学生新升级 {len(actual)} 项"
        assert body.startswith(header), body
        for student_id, name, seq in actual:
            line = f"- 学生 {student_id} 里程碑「{name}」（第 {seq} 次升级）"
            assert line in body, f"汇总缺少: {line}"
        # 行数也不能多：正文行数 == 实际升级数
        lines = [ln for ln in body.splitlines() if ln.startswith("- ")]
        assert len(lines) == len(actual)


def test_admin_summaries_match_escalation_counts(db_with_concurrent_ops):
    conn = sqlite3.connect(db_with_concurrent_ops)
    summaries = conn.execute(
        "SELECT recipient_id, eval_date, body FROM notifications"
        " WHERE kind = 'admin_summary'"
    ).fetchall()
    admins = [r[0] for r in conn.execute("SELECT id FROM admins")]
    assert sorted(r[0] for r in summaries) == sorted(admins)
    for _, eval_date, body in summaries:
        total = conn.execute(
            "SELECT COUNT(*) FROM escalations WHERE eval_date=?",
            (eval_date,),
        ).fetchone()[0]
        assert body == f"【全区汇总】评估日 {eval_date}：全区新升级共 {total} 项"


def test_every_counselor_and_admin_got_summary(db_with_concurrent_ops):
    conn = sqlite3.connect(db_with_concurrent_ops)
    for kind, table in [("counselor_summary", "counselors"), ("admin_summary", "admins")]:
        missing = conn.execute(
            f"SELECT id FROM {table} t WHERE NOT EXISTS ("
            f"  SELECT 1 FROM notifications n"
            f"  WHERE n.recipient_id = t.id AND n.kind = ? AND n.eval_date = ?)",
            (kind, EVAL_DATE),
        ).fetchall()
        assert missing == [], f"{kind} 缺失: {missing}"
