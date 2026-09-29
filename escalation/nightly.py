"""夜间评估任务：可中断、可补跑、幂等。

崩溃恢复原理：
- 每个评估日在 evaluations 表中有一行，记录阶段与游标。
- 评估阶段按里程碑 id 顺序分批处理，每批一个 IMMEDIATE 事务，
  提交后推进游标；被杀后从游标处续跑，已提交的批次不会重做。
- 汇总阶段同理，先辅导员后管理员，各自按 id 游标推进；
  通知表上的唯一索引保证即使游标丢失也不会重复发送。
- 汇总内容从 escalations 表推导，因此永远与升级通知对得上。
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

from .db import connect, init_schema

OVERDUE_AFTER_DAYS = 30

ProgressHook = "callable[[str, str], None]"


def _is_overdue(due_date: str, eval_date: str) -> bool:
    """评估日不早于截止日期后第 30 个自然日（第 29 天不算）。"""
    return (date.fromisoformat(eval_date) - date.fromisoformat(due_date)).days >= OVERDUE_AFTER_DAYS


def run_evaluation(
    db_path: str | Path,
    eval_date: str,
    *,
    batch_size: int = 500,
    progress_hook=None,
) -> None:
    """运行评估日 eval_date 的评估。

    - 存在未完成的更早评估日时，先补完它（含其汇总），再跑本日。
    - 已完整跑完的评估日再次调用为 no-op。
    - 早于最近已完成评估日（或早于一个未完成的评估日）的请求抛 ValueError。
    - progress_hook(phase, eval_date)：每批提交后回调，测试用来模拟杀进程。
    """
    conn = connect(db_path)
    try:
        init_schema(conn)
        # 先补完所有不晚于 eval_date 的未完成评估日
        while True:
            row = conn.execute(
                "SELECT eval_date FROM evaluations WHERE status != 'completed'"
                " ORDER BY eval_date LIMIT 1"
            ).fetchone()
            if row is None:
                break
            if row["eval_date"] > eval_date:
                raise ValueError(
                    f"评估日 {eval_date} 早于未完成的评估日 {row['eval_date']}"
                )
            _run_one(conn, row["eval_date"], batch_size, progress_hook)

        # 处理 eval_date 本身
        row = conn.execute(
            "SELECT status FROM evaluations WHERE eval_date=?", (eval_date,)
        ).fetchone()
        if row is not None:
            return  # 已完整跑完，no-op
        last_done = conn.execute(
            "SELECT MAX(eval_date) AS d FROM evaluations WHERE status='completed'"
        ).fetchone()["d"]
        if last_done is not None and eval_date < last_done:
            raise ValueError(f"评估日 {eval_date} 早于已完成的评估日 {last_done}")
        with conn:
            conn.execute(
                "INSERT INTO evaluations(eval_date, status) VALUES (?, 'evaluating')",
                (eval_date,),
            )
        _run_one(conn, eval_date, batch_size, progress_hook)
    finally:
        conn.close()


def _run_one(conn, eval_date: str, batch_size: int, hook) -> None:
    row = conn.execute(
        "SELECT status, cursor FROM evaluations WHERE eval_date=?", (eval_date,)
    ).fetchone()
    if row["status"] == "completed":
        return
    if row["status"] == "evaluating":
        _eval_phase(conn, eval_date, row["cursor"], batch_size, hook)
    _summary_phase(conn, eval_date, batch_size, hook)
    with conn:
        conn.execute(
            "UPDATE evaluations SET status='completed' WHERE eval_date=?",
            (eval_date,),
        )
    if hook:
        hook("completed", eval_date)


def _eval_phase(conn, eval_date: str, cursor: str, batch_size: int, hook) -> None:
    while True:
        conn.execute("BEGIN IMMEDIATE")
        try:
            rows = conn.execute(
                "SELECT id, due_date, status FROM milestones"
                " WHERE id > ? ORDER BY id LIMIT ?",
                (cursor, batch_size),
            ).fetchall()
            if not rows:
                conn.execute(
                    "UPDATE evaluations SET status='summarizing' WHERE eval_date=?",
                    (eval_date,),
                )
                conn.commit()
                return
            for r in rows:
                if r["status"] == "pending" and _is_overdue(r["due_date"], eval_date):
                    _escalate(conn, r["id"], eval_date)
            cursor = rows[-1]["id"]
            conn.execute(
                "UPDATE evaluations SET cursor=? WHERE eval_date=?",
                (cursor, eval_date),
            )
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        if hook:
            hook("eval_batch", eval_date)


def _escalate(conn, milestone_id: str, eval_date: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO escalations(milestone_id, eval_date) VALUES (?, ?)",
        (milestone_id, eval_date),
    )
    conn.execute(
        "UPDATE milestones SET status='overdue' WHERE id=?", (milestone_id,)
    )
    esc_id = conn.execute(
        "SELECT id FROM escalations WHERE milestone_id=? AND eval_date=?",
        (milestone_id, eval_date),
    ).fetchone()["id"]
    info = conn.execute(
        "SELECT m.kind, m.due_date, m.student_id, s.counselor_id"
        " FROM milestones m JOIN students s ON s.id = m.student_id"
        " WHERE m.id=?",
        (milestone_id,),
    ).fetchone()
    body = (
        f"学生 {info['student_id']} 的里程碑 {info['kind']}"
        f"（截止 {info['due_date']}）于 {eval_date} 被标记为落后"
    )
    recipients = [info["counselor_id"]] + [
        r["id"] for r in conn.execute("SELECT id FROM admins ORDER BY id")
    ]
    for rec in recipients:
        conn.execute(
            "INSERT OR IGNORE INTO notifications"
            "(recipient_id, kind, eval_date, escalation_id, body)"
            " VALUES (?, 'escalation', ?, ?, ?)",
            (rec, eval_date, esc_id, body),
        )


def _summary_phase(conn, eval_date: str, batch_size: int, hook) -> None:
    while True:
        row = conn.execute(
            "SELECT summary_scope, summary_cursor FROM evaluations WHERE eval_date=?",
            (eval_date,),
        ).fetchone()
        scope, cursor = row["summary_scope"], row["summary_cursor"]
        if scope == "done":
            return
        table = "counselors" if scope == "counselor" else "admins"
        conn.execute("BEGIN IMMEDIATE")
        try:
            recipients = conn.execute(
                f"SELECT id FROM {table} WHERE id > ? ORDER BY id LIMIT ?",
                (cursor, batch_size),
            ).fetchall()
            if not recipients:
                next_scope = "admin" if scope == "counselor" else "done"
                conn.execute(
                    "UPDATE evaluations SET summary_scope=?, summary_cursor=''"
                    " WHERE eval_date=?",
                    (next_scope, eval_date),
                )
            else:
                for rec in recipients:
                    _send_summary(conn, eval_date, scope, rec["id"])
                conn.execute(
                    "UPDATE evaluations SET summary_cursor=? WHERE eval_date=?",
                    (recipients[-1]["id"], eval_date),
                )
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        if hook:
            hook("summary_batch", eval_date)


def _send_summary(conn, eval_date: str, scope: str, recipient_id: str) -> None:
    if scope == "counselor":
        rows = conn.execute(
            "SELECT m.id AS mid, m.kind, m.due_date, m.student_id"
            " FROM escalations e"
            " JOIN milestones m ON m.id = e.milestone_id"
            " JOIN students s ON s.id = m.student_id"
            " WHERE e.eval_date=? AND s.counselor_id=? ORDER BY e.id",
            (eval_date, recipient_id),
        ).fetchall()
        lines = [
            f"评估日 {eval_date}：名下学生新升级 {len(rows)} 项",
        ] + [
            f"- 学生 {r['student_id']} 里程碑 {r['kind']}（截止 {r['due_date']}）"
            for r in rows
        ]
        kind = "counselor_summary"
    else:
        n = conn.execute(
            "SELECT COUNT(*) AS c FROM escalations WHERE eval_date=?",
            (eval_date,),
        ).fetchone()["c"]
        lines = [f"评估日 {eval_date}：全区新升级 {n} 项"]
        kind = "admin_summary"
    conn.execute(
        "INSERT OR IGNORE INTO notifications"
        "(recipient_id, kind, eval_date, escalation_id, body)"
        " VALUES (?, ?, ?, NULL, ?)",
        (recipient_id, kind, eval_date, "\n".join(lines)),
    )
