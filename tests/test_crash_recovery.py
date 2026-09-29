"""在夜间任务的不同位置杀掉进程后补跑，结果与一次跑完逐条一致。"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

from escalation import run_evaluation
from escalation.db import connect

from .helpers import assert_same_data, seed_service

EVAL_DATE = "2026-02-15"
BATCH = 7
# 360 个里程碑 -> 52 个评估批；之后是汇总批与完成标记。
# 覆盖：评估前期/中期/末期、汇总发到一半、即将完成。
KILL_POINTS = [1, 5, 20, 51, 52, 53, 54, 55, 56, 57, 58, 60]


def _seed(db: str) -> None:
    seed_service(db, n_counselors=3, students_per=20, milestones_per=6, n_admins=2)


@pytest.fixture()
def ref_db(tmp_path) -> str:
    db = str(tmp_path / "ref.db")
    _seed(db)
    run_evaluation(db, EVAL_DATE, batch_size=BATCH)
    return db


def _crash_run(db: str, kill_at: int) -> None:
    runner = Path(__file__).parent / "crash_runner.py"
    env = dict(os.environ)
    root = str(Path(__file__).parent.parent)
    env["PYTHONPATH"] = root + os.pathsep + env.get("PYTHONPATH", "")
    subprocess.run(
        [sys.executable, str(runner), db, EVAL_DATE, str(kill_at), str(BATCH)],
        check=False,  # 被杀时退出码非 0
        env=env,
    )


@pytest.mark.parametrize("kill_at", KILL_POINTS)
def test_crash_and_resume_matches_one_shot(tmp_path, ref_db, kill_at):
    db = str(tmp_path / "crash.db")
    _seed(db)
    _crash_run(db, kill_at)
    run_evaluation(db, EVAL_DATE, batch_size=BATCH)  # 补跑
    assert_same_data(db, ref_db)


def test_crash_mid_summary_then_read_status_preserved(tmp_path, ref_db):
    db = str(tmp_path / "crash.db")
    _seed(db)
    # 杀在汇总阶段中间（评估 52 批 + 辅导员汇总 1 批之后）
    _crash_run(db, 54)

    # 补跑前，把已发出的通知标几条为已读
    conn = connect(db)
    ids = [r["id"] for r in conn.execute(
        "SELECT id FROM notifications ORDER BY id LIMIT 10"
    )]
    assert ids, "崩溃前应已有部分通知落库"
    with conn:
        conn.executemany(
            "UPDATE notifications SET read=1 WHERE id=?", [(i,) for i in ids]
        )
    conn.close()

    run_evaluation(db, EVAL_DATE, batch_size=BATCH)  # 补跑

    # 已读状态不丢
    conn = connect(db)
    read_ids = {r["id"] for r in conn.execute(
        "SELECT id FROM notifications WHERE read=1"
    )}
    conn.close()
    assert set(ids) <= read_ids

    # 参照库施加同样的已读标记后，两库逐条一致
    conn = connect(ref_db)
    with conn:
        conn.executemany(
            "UPDATE notifications SET read=1 WHERE id=?", [(i,) for i in ids]
        )
    conn.close()
    assert_same_data(db, ref_db)
