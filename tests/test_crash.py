"""崩溃恢复测试：在夜间任务的不同位置杀掉进程，补跑结果须与一次跑完一致。"""

import os
import sqlite3
import subprocess
import sys

import pytest

from conftest import make_service

WORKER = os.path.join(os.path.dirname(__file__), "crash_worker.py")

EVAL_DATE = "2026-02-05"

CRASH_LABELS = [
    "judge_batch:1",              # 判定第一批提交后
    "judge_batch:3",              # 判定中途
    "judging_done",               # 判定全部完成、汇总未开始
    "counselor_summary_batch:1",  # 辅导员汇总发到一半
    "counselor_summary_batch:3",
    "counselor_summaries_done",   # 辅导员汇总发完
    "admin_summary_batch:1",      # 管理员汇总发到一半
    "admin_summaries_done",       # 全部提交完、刚好收尾
]


def build_dataset(db_path):
    """40 名学生 x 5 个里程碑；截止日错开，评估日只有一部分进入落后。"""
    from escalation import EscalationService

    svc = EscalationService(db_path, admins=[f"admin{i}" for i in range(1, 6)])
    svc.add_students((f"s{i}", f"c{i % 20}") for i in range(40))
    svc.add_milestones(
        (f"m{i}_{k}", f"s{i}", f"里程碑{k}", f"2026-01-{(i + k) % 28 + 1:02d}")
        for i in range(40)
        for k in range(5)
    )
    # 多几个管理员，让管理员汇总也分批
    svc.conn.executemany(
        "INSERT OR IGNORE INTO admins(id) VALUES (?)",
        [(f"admin{i}",) for i in range(3, 6)],
    )
    svc.conn.close()


def run_worker(db_path, eval_date, crash_at=None):
    env = dict(
        os.environ,
        ESCALATION_JUDGE_BATCH="50",
        ESCALATION_SUMMARY_BATCH="2",
    )
    if crash_at:
        env["ESCALATION_CRASH_AT"] = crash_at
    return subprocess.run(
        [sys.executable, WORKER, db_path, eval_date],
        env=env,
        capture_output=True,
        text=True,
    )


def dump_state(db_path):
    conn = sqlite3.connect(db_path)
    return {
        "milestones": sorted(
            conn.execute(
                "SELECT id, status, escalation_seq, due_date FROM milestones"
            ).fetchall()
        ),
        "escalations": sorted(
            conn.execute(
                "SELECT milestone_id, seq, eval_date FROM escalations"
            ).fetchall()
        ),
        "notifications": sorted(
            conn.execute(
                "SELECT recipient_id, kind, eval_date, body FROM notifications"
            ).fetchall()
        ),
        "runs": sorted(
            conn.execute("SELECT eval_date, phase FROM eval_runs").fetchall()
        ),
        "meta": sorted(conn.execute("SELECT key, value FROM meta").fetchall()),
    }


@pytest.fixture
def reference_state(tmp_path):
    db = str(tmp_path / "ref.db")
    build_dataset(db)
    result = run_worker(db, EVAL_DATE)
    assert result.returncode == 0, result.stderr
    return dump_state(db)


@pytest.mark.parametrize("crash_at", CRASH_LABELS)
def test_crash_then_resume_matches_clean_run(tmp_path, reference_state, crash_at):
    db = str(tmp_path / "crash.db")
    build_dataset(db)
    killed = run_worker(db, EVAL_DATE, crash_at=crash_at)
    assert killed.returncode != 0, f"worker should have died at {crash_at}"
    # 补跑（新的进程、同一个数据库文件）
    resumed = run_worker(db, EVAL_DATE)
    assert resumed.returncode == 0, resumed.stderr
    assert dump_state(db) == reference_state


def test_read_status_survives_crash_and_resume(tmp_path):
    db = str(tmp_path / "crash.db")
    build_dataset(db)
    killed = run_worker(db, EVAL_DATE, crash_at="counselor_summary_batch:1")
    assert killed.returncode != 0
    # 补跑前把已发出的通知标为已读
    from escalation import EscalationService

    svc = EscalationService(db)
    marked = []
    for recipient in ["c0", "admin1"]:
        for n in svc.list_notifications(recipient):
            svc.mark_read(n["id"], recipient)
            marked.append((recipient, n["kind"], n["eval_date"], n["body"]))
    assert marked, "崩溃前应有部分通知已发出"
    svc.conn.close()
    # 补跑
    resumed = run_worker(db, EVAL_DATE)
    assert resumed.returncode == 0, resumed.stderr
    # 已读状态不丢
    svc = EscalationService(db)
    for recipient, kind, eval_date, body in marked:
        match = [
            n
            for n in svc.list_notifications(recipient)
            if n["kind"] == kind
            and n["eval_date"] == eval_date
            and n["body"] == body
        ]
        assert len(match) == 1
        assert match[0]["read"] == 1


def test_interrupted_date_completed_when_next_date_arrives(tmp_path):
    """D1 中途死掉没补跑，D2 到来时先补完 D1 再跑 D2，两天汇总各归各的。"""
    d1, d2 = "2026-02-05", "2026-02-06"
    crash_db = str(tmp_path / "crash.db")
    ref_db = str(tmp_path / "ref.db")
    build_dataset(crash_db)
    build_dataset(ref_db)
    # 参考：两天各自干净跑完
    assert run_worker(ref_db, d1).returncode == 0
    assert run_worker(ref_db, d2).returncode == 0
    # 事故：D1 判定中途死掉，直接来 D2
    assert run_worker(crash_db, d1, crash_at="judge_batch:2").returncode != 0
    result = run_worker(crash_db, d2)
    assert result.returncode == 0, result.stderr
    assert dump_state(crash_db) == dump_state(ref_db)
    # 两天的汇总各自带各自的日期
    conn = sqlite3.connect(crash_db)
    summaries = conn.execute(
        "SELECT DISTINCT eval_date FROM notifications"
        " WHERE kind = 'counselor_summary'"
    ).fetchall()
    assert sorted(r[0] for r in summaries) == [d1, d2]


def test_never_started_dates_are_not_backfilled(tmp_path):
    """没运行过的日期不补：跑 02-05 再跑 02-07，不补 02-06。"""
    db = str(tmp_path / "gap.db")
    build_dataset(db)
    assert run_worker(db, "2026-02-05").returncode == 0
    assert run_worker(db, "2026-02-07").returncode == 0
    conn = sqlite3.connect(db)
    dates = sorted(
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT eval_date FROM notifications"
            " WHERE kind = 'counselor_summary'"
        )
    )
    assert dates == ["2026-02-05", "2026-02-07"]
