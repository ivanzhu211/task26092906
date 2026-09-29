"""中断的评估日在下一个评估日运行时被补完，两天的汇总各归各的日期。"""
import os
import subprocess
import sys
from pathlib import Path

from escalation import EscalationService, run_evaluation
from escalation.db import connect

from .helpers import seed_service

DAY1 = "2026-02-10"
DAY2 = "2026-02-12"


def _crash_day1(db: str) -> None:
    runner = Path(__file__).parent / "crash_runner.py"
    env = dict(os.environ)
    root = str(Path(__file__).parent.parent)
    env["PYTHONPATH"] = root + os.pathsep + env.get("PYTHONPATH", "")
    # 评估批中途杀掉（360 里程碑 / 7 = 52 批，杀在第 10 批后）
    subprocess.run(
        [sys.executable, str(runner), db, DAY1, "10", "7"],
        check=False, env=env,
    )


def test_interrupted_day_resumed_on_next_day(tmp_path):
    db = str(tmp_path / "t.db")
    seed_service(db)
    _crash_day1(db)

    # 确认 DAY1 处于中断状态
    conn = connect(db)
    assert conn.execute(
        "SELECT status FROM evaluations WHERE eval_date=?", (DAY1,)
    ).fetchone()[0] != "completed"
    conn.close()

    # 运行 DAY2：应先补完 DAY1（含其汇总），再跑 DAY2
    run_evaluation(db, DAY2, batch_size=7)

    conn = connect(db)
    statuses = dict(conn.execute(
        "SELECT eval_date, status FROM evaluations"
    ).fetchall())
    assert statuses == {DAY1: "completed", DAY2: "completed"}
    # 没运行过的日期不补
    assert "2026-02-11" not in statuses
    conn.close()

    svc = EscalationService(db)
    # 两天的汇总各归各的日期，数量与当天升级通知一致
    for day in (DAY1, DAY2):
        esc = [n for n in svc.get_notifications("admin1")
               if n.kind == "escalation" and n.eval_date == day]
        summ = [n for n in svc.get_notifications("admin1")
                if n.kind == "admin_summary" and n.eval_date == day]
        assert len(summ) == 1
        assert f"评估日 {day}：全区新升级 {len(esc)} 项" in summ[0].body
        cs = [n for n in svc.get_notifications("coun0")
              if n.kind == "counselor_summary" and n.eval_date == day]
        assert len(cs) == 1
        own = [n for n in svc.get_notifications("coun0")
               if n.kind == "escalation" and n.eval_date == day]
        assert f"名下学生新升级 {len(own)} 项" in cs[0].body


def test_interrupted_day_result_matches_one_shot(tmp_path):
    """中断后由下一日补跑，与两天各自一次跑完的结果逐条一致。"""
    db = str(tmp_path / "crash.db")
    seed_service(db)
    _crash_day1(db)
    run_evaluation(db, DAY2, batch_size=7)

    ref = str(tmp_path / "ref.db")
    seed_service(ref)
    run_evaluation(ref, DAY1, batch_size=7)
    run_evaluation(ref, DAY2, batch_size=7)

    from .helpers import assert_same_data
    assert_same_data(db, ref)
