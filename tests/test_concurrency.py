"""夜间任务运行时另一进程不断操作：操作都要快，结束后不变量成立。"""
import multiprocessing as mp
import random
import re
import time

from escalation import EscalationService, run_evaluation
from escalation.db import connect

from .helpers import seed_service

EVAL_DATE = "2026-02-15"
N_ADMINS = 2


def _ops_worker(db: str, milestone_ids: list[str], stop, queue) -> None:
    """另一个进程：随机标记完成/撤销完成/改截止日期，记录每次耗时。"""
    svc = EscalationService(db)
    rng = random.Random(42)
    max_latency = 0.0
    n_ops = 0
    while not stop.is_set():
        mid = rng.choice(milestone_ids)
        op = rng.choice(["mark", "unmark", "due"])
        start = time.monotonic()
        try:
            if op == "mark":
                svc.mark_complete(mid)
            elif op == "unmark":
                svc.unmark_complete(mid)
            else:
                svc.change_due_date(mid, "2026-02-01")
        except KeyError:
            continue  # 状态不允许的操作（如对未完成的撤销），忽略
        max_latency = max(max_latency, time.monotonic() - start)
        n_ops += 1
    queue.put((n_ops, max_latency))


def _nightly_worker(db: str) -> None:
    def slow_hook(phase, d):
        time.sleep(0.002)  # 拉大并发窗口
    run_evaluation(db, EVAL_DATE, batch_size=20, progress_hook=slow_hook)


def test_concurrent_ops_during_nightly(tmp_path):
    db = str(tmp_path / "t.db")
    seed_service(db, n_counselors=4, students_per=50, milestones_per=6,
                 n_admins=N_ADMINS)
    conn = connect(db)
    milestone_ids = [r["id"] for r in conn.execute("SELECT id FROM milestones")]
    conn.close()

    ctx = mp.get_context("spawn")
    stop = ctx.Event()
    queue = ctx.Queue()
    ops = ctx.Process(target=_ops_worker, args=(db, milestone_ids, stop, queue))
    nightly = ctx.Process(target=_nightly_worker, args=(db,))
    ops.start()
    nightly.start()
    nightly.join(timeout=300)
    assert not nightly.is_alive(), "夜间任务超时"
    stop.set()
    ops.join(timeout=30)
    assert nightly.exitcode == 0 and ops.exitcode == 0

    n_ops, max_latency = queue.get()
    assert n_ops > 50, f"并发操作数太少: {n_ops}"
    assert max_latency < 1.0, f"有操作超过1秒: {max_latency:.3f}s"

    _check_invariants(db)


def _check_invariants(db: str) -> None:
    conn = connect(db)
    admins = {r["id"] for r in conn.execute("SELECT id FROM admins")}
    assert len(admins) == N_ADMINS

    # 1) 已完成的里程碑不能是落后（单状态列天然保证，这里显式断言数据一致）
    assert conn.execute(
        "SELECT COUNT(*) c FROM milestones WHERE status NOT IN"
        " ('pending','completed','overdue')"
    ).fetchone()["c"] == 0

    # 2) 每个落后的里程碑：对应"这次升级"（最近一次）对每位接收人恰好一条通知
    overdue = conn.execute(
        "SELECT m.id, s.counselor_id FROM milestones m"
        " JOIN students s ON s.id=m.student_id WHERE m.status='overdue'"
    ).fetchall()
    for row in overdue:
        esc = conn.execute(
            "SELECT id FROM escalations WHERE milestone_id=? ORDER BY id DESC"
            " LIMIT 1", (row["id"],)
        ).fetchone()
        recips = [r["recipient_id"] for r in conn.execute(
            "SELECT recipient_id FROM notifications WHERE escalation_id=?",
            (esc["id"],),
        )]
        assert sorted(recips) == sorted([row["counselor_id"], *admins]), (
            f"里程碑 {row['id']} 的升级通知接收人不齐或有重复"
        )

    # 3) 每次升级的通知接收人集合都恰好是 负责辅导员+全部管理员
    orphan = conn.execute(
        "SELECT e.id FROM escalations e WHERE ("
        "  SELECT COUNT(*) FROM notifications n WHERE n.escalation_id=e.id"
        " ) != ?", (1 + N_ADMINS,)
    ).fetchall()
    assert not orphan, f"有升级的通知条数不对: {orphan[:3]}"

    # 4) 每条汇总与当天实际发出的升级通知对得上
    for n in conn.execute(
        "SELECT recipient_id, kind, eval_date, body FROM notifications"
        " WHERE kind IN ('counselor_summary','admin_summary')"
    ):
        declared = int(re.search(r"新升级 (\d+) 项", n["body"]).group(1))
        if n["kind"] == "counselor_summary":
            actual = conn.execute(
                "SELECT COUNT(*) c FROM notifications x"
                " JOIN escalations e ON e.id=x.escalation_id"
                " JOIN milestones m ON m.id=e.milestone_id"
                " JOIN students s ON s.id=m.student_id"
                " WHERE x.kind='escalation' AND x.eval_date=?"
                " AND s.counselor_id=? AND x.recipient_id=?",
                (n["eval_date"], n["recipient_id"], n["recipient_id"]),
            ).fetchone()["c"]
        else:
            actual = conn.execute(
                "SELECT COUNT(*) c FROM escalations WHERE eval_date=?",
                (n["eval_date"],),
            ).fetchone()["c"]
        assert declared == actual, (
            f"{n['kind']} {n['recipient_id']} {n['eval_date']}:"
            f" 汇总 {declared} != 实际 {actual}"
        )
    conn.close()
