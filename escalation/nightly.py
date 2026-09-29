"""夜间评估：判定落后、发升级通知、发汇总。全程可崩溃续跑。

崩溃恢复不变式：
- 判定按批提交，每批内「改状态 + 建升级 + 发通知 + 记判定」同一事务，
  要么全生效要么全回滚；eval_judgments 保证同一评估日每个里程碑只判一次。
- 汇总在判定全部完成后生成，内容只依赖 escalations 表，重算结果确定；
  用 INSERT OR IGNORE + 唯一索引去重，发到一半被杀可安全重发。
- phase 推进与水位线更新各自成事务，崩溃后从最近阶段继续。
"""

import os
import time
from datetime import date as date_type, timedelta

from .db import META_LAST_COMPLETED_EVAL_DATE

JUDGE_BATCH_SIZE = int(os.environ.get("ESCALATION_JUDGE_BATCH", "2000"))
SUMMARY_BATCH_SIZE = int(os.environ.get("ESCALATION_SUMMARY_BATCH", "500"))

# 每批提交后让出片刻，保证共用同一数据库文件的辅导员操作不被饿死、
# 能在 1 秒内拿到写锁。300k 里程碑约 150 批，总让出时间不到 1 秒。
BATCH_PAUSE_SECONDS = (
    float(os.environ.get("ESCALATION_BATCH_PAUSE_MS", "5")) / 1000
)

OVERDUE_GRACE_DAYS = 30

def env_crash_hook():
    """测试用：按环境变量 ESCALATION_CRASH_AT 在检查点自杀（模拟 kill -9）。

    取值如 "judge_batch"（首次到达即死）或 "judge_batch:3"（第 3 次到达才死）。
    """
    spec = os.environ.get("ESCALATION_CRASH_AT")
    counters = {}

    def hook(label):
        if not spec:
            return
        counters[label] = counters.get(label, 0) + 1
        if spec == label or spec == f"{label}:{counters[label]}":
            os._exit(1)

    return hook


def run_evaluation(conn, tx, eval_date, crash_hook=None):
    """跑评估日 eval_date；若之前有未完成的评估日，先补完它（含其汇总）。

    返回本次实际完成的评估日列表（含补跑的）。
    """
    if crash_hook is None:
        crash_hook = env_crash_hook()

    with tx():
        last = _meta(conn, META_LAST_COMPLETED_EVAL_DATE)
        run = conn.execute(
            "SELECT phase FROM eval_runs WHERE eval_date=?", (eval_date,)
        ).fetchone()
        if run is not None and run["phase"] == "done":
            return []  # 已完整跑过：什么也不做
        if last is not None and eval_date < last:
            from .service import EvaluationDateError

            raise EvaluationDateError(
                f"评估日 {eval_date} 早于已完成评估日 {last}"
            )
        if run is None:
            conn.execute(
                "INSERT INTO eval_runs(eval_date, phase) VALUES (?, 'judging')",
                (eval_date,),
            )
        pending = [
            row["eval_date"]
            for row in conn.execute(
                "SELECT eval_date FROM eval_runs"
                " WHERE phase != 'done' AND eval_date <= ? ORDER BY eval_date",
                (eval_date,),
            )
        ]

    for date in pending:
        _complete_run(conn, tx, date, crash_hook)
    return pending


def _meta(conn, key):
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def _phase(conn, eval_date):
    return conn.execute(
        "SELECT phase FROM eval_runs WHERE eval_date=?", (eval_date,)
    ).fetchone()["phase"]


def _set_phase(conn, eval_date, phase):
    conn.execute(
        "UPDATE eval_runs SET phase=? WHERE eval_date=?", (phase, eval_date)
    )


def _complete_run(conn, tx, eval_date, crash_hook):
    phase = _phase(conn, eval_date)
    if phase == "judging":
        _judge_all(conn, tx, eval_date, crash_hook)
        with tx():
            _set_phase(conn, eval_date, "counselor_summaries")
        crash_hook("judging_done")
        phase = "counselor_summaries"
    if phase == "counselor_summaries":
        _counselor_summaries(conn, tx, eval_date, crash_hook)
        with tx():
            _set_phase(conn, eval_date, "admin_summaries")
        crash_hook("counselor_summaries_done")
        phase = "admin_summaries"
    if phase == "admin_summaries":
        _admin_summaries(conn, tx, eval_date, crash_hook)
        with tx():
            _set_phase(conn, eval_date, "done")
            conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
                (META_LAST_COMPLETED_EVAL_DATE, eval_date),
            )
        crash_hook("admin_summaries_done")


def _judge_all(conn, tx, eval_date, crash_hook):
    admins = [r["id"] for r in conn.execute("SELECT id FROM admins ORDER BY id")]
    eval_day = date_type.fromisoformat(eval_date)
    while True:
        with tx():
            cursor = conn.execute(
                "SELECT judge_cursor FROM eval_runs WHERE eval_date=?",
                (eval_date,),
            ).fetchone()["judge_cursor"]
            rows = conn.execute(
                "SELECT m.rowid AS rid, m.id, m.status, m.due_date,"
                "       m.escalation_seq, m.name, m.student_id, s.counselor_id"
                " FROM milestones m JOIN students s ON s.id = m.student_id"
                " WHERE m.rowid > ?"
                "   AND NOT EXISTS (SELECT 1 FROM eval_judgments j"
                "                   WHERE j.eval_date = ? AND j.milestone_id = m.id)"
                " ORDER BY m.rowid LIMIT ?",
                (cursor, eval_date, JUDGE_BATCH_SIZE),
            ).fetchall()
            if not rows:
                return
            for row in rows:
                overdue = (
                    row["status"] == "pending"
                    and date_type.fromisoformat(row["due_date"])
                    + timedelta(days=OVERDUE_GRACE_DAYS)
                    <= eval_day
                )
                if overdue:
                    seq = row["escalation_seq"] + 1
                    conn.execute(
                        "UPDATE milestones SET status='overdue', escalation_seq=?"
                        " WHERE id=?",
                        (seq, row["id"]),
                    )
                    esc_id = conn.execute(
                        "INSERT INTO escalations(milestone_id, seq, eval_date)"
                        " VALUES (?, ?, ?)",
                        (row["id"], seq, eval_date),
                    ).lastrowid
                    body = (
                        f"【升级】学生 {row['student_id']} 的里程碑"
                        f"「{row['name']}」已落后"
                        f"（第 {seq} 次升级，评估日 {eval_date}）"
                    )
                    for recipient in [row["counselor_id"], *admins]:
                        conn.execute(
                            "INSERT INTO notifications"
                            " (recipient_id, kind, eval_date, escalation_id, body)"
                            " VALUES (?, 'escalation', ?, ?, ?)",
                            (recipient, eval_date, esc_id, body),
                        )
                conn.execute(
                    "INSERT INTO eval_judgments(eval_date, milestone_id)"
                    " VALUES (?, ?)",
                    (eval_date, row["id"]),
                )
            conn.execute(
                "UPDATE eval_runs SET judge_cursor=? WHERE eval_date=?",
                (rows[-1]["rid"], eval_date),
            )
        crash_hook("judge_batch")
        time.sleep(BATCH_PAUSE_SECONDS)


def _chunks(items, size):
    for i in range(0, len(items), size):
        yield items[i : i + size]


def _counselor_summaries(conn, tx, eval_date, crash_hook):
    counselors = [
        r["id"] for r in conn.execute("SELECT id FROM counselors ORDER BY id")
    ]
    for chunk in _chunks(counselors, SUMMARY_BATCH_SIZE):
        with tx():
            for cid in chunk:
                lines = [
                    f"- 学生 {r['student_id']} 里程碑「{r['name']}」"
                    f"（第 {r['seq']} 次升级）"
                    for r in conn.execute(
                        "SELECT e.seq, m.name, m.student_id"
                        " FROM escalations e"
                        " JOIN milestones m ON m.id = e.milestone_id"
                        " JOIN students s ON s.id = m.student_id"
                        " WHERE e.eval_date = ? AND s.counselor_id = ?"
                        " ORDER BY e.id",
                        (eval_date, cid),
                    )
                ]
                body = (
                    f"【汇总】评估日 {eval_date}：名下学生新升级 {len(lines)} 项"
                    + ("\n" + "\n".join(lines) if lines else "")
                )
                conn.execute(
                    "INSERT OR IGNORE INTO notifications"
                    " (recipient_id, kind, eval_date, body)"
                    " VALUES (?, 'counselor_summary', ?, ?)",
                    (cid, eval_date, body),
                )
        crash_hook("counselor_summary_batch")
        time.sleep(BATCH_PAUSE_SECONDS)


def _admin_summaries(conn, tx, eval_date, crash_hook):
    total = conn.execute(
        "SELECT COUNT(*) AS n FROM escalations WHERE eval_date=?", (eval_date,)
    ).fetchone()["n"]
    body = f"【全区汇总】评估日 {eval_date}：全区新升级共 {total} 项"
    admins = [r["id"] for r in conn.execute("SELECT id FROM admins ORDER BY id")]
    for chunk in _chunks(admins, SUMMARY_BATCH_SIZE):
        with tx():
            for aid in chunk:
                conn.execute(
                    "INSERT OR IGNORE INTO notifications"
                    " (recipient_id, kind, eval_date, body)"
                    " VALUES (?, 'admin_summary', ?, ?)",
                    (aid, eval_date, body),
                )
        crash_hook("admin_summary_batch")
        time.sleep(BATCH_PAUSE_SECONDS)
