"""业务规则测试：判定边界、状态操作、升级与汇总、评估日单调性。"""

import pytest

from escalation import EvaluationDateError, InvalidOperationError

from conftest import ADMINS, make_service


# ---- 第 29 / 30 天边界 ----


@pytest.mark.parametrize(
    "eval_date,expected",
    [
        ("2026-01-29", "pending"),   # 第 28 天
        ("2026-01-30", "pending"),   # 第 29 天：不算落后
        ("2026-01-31", "overdue"),   # 第 30 天：落后
        ("2026-02-15", "overdue"),   # 更晚也算
    ],
)
def test_overdue_boundary(db_path, eval_date, expected):
    svc = make_service(db_path, n_students=1, n_milestones=1, due_date="2026-01-01")
    svc.run_evaluation(eval_date)
    assert svc.milestone_status("m0_0") == expected


def test_day29_then_day30_same_milestone(db_path):
    svc = make_service(db_path, n_students=1, n_milestones=1, due_date="2026-01-03")
    svc.run_evaluation("2026-02-01")  # 第 29 天
    assert svc.milestone_status("m0_0") == "pending"
    svc.run_evaluation("2026-02-02")  # 第 30 天
    assert svc.milestone_status("m0_0") == "overdue"


# ---- 辅导员操作 ----


def test_mark_completed_and_undo(service):
    service.add_students([("s1", "c1")])
    service.add_milestones([("m1", "s1", "PSAT", "2026-01-01")])
    service.mark_completed("m1")
    assert service.milestone_status("m1") == "completed"
    service.mark_completed("m1")  # 幂等
    assert service.milestone_status("m1") == "completed"
    service.undo_completed("m1")
    assert service.milestone_status("m1") == "pending"


def test_undo_requires_completed(service):
    service.add_students([("s1", "c1")])
    service.add_milestones([("m1", "s1", "PSAT", "2026-01-01")])
    with pytest.raises(InvalidOperationError):
        service.undo_completed("m1")


def test_completed_milestone_never_judged_overdue(service):
    service.add_students([("s1", "c1")])
    service.add_milestones([("m1", "s1", "PSAT", "2026-01-01")])
    service.mark_completed("m1")
    service.run_evaluation("2026-06-01")
    assert service.milestone_status("m1") == "completed"
    assert service.list_notifications("c1")[-1]["kind"] == "counselor_summary"
    assert not any(
        n["kind"] == "escalation" for n in service.list_notifications("c1")
    )


def test_ops_never_set_overdue(service):
    service.add_students([("s1", "c1")])
    service.add_milestones([("m1", "s1", "PSAT", "2026-01-01")])
    service.change_due_date("m1", "2025-01-01")  # 改成早已过去的日期
    assert service.milestone_status("m1") == "pending"
    service.mark_completed("m1")
    service.change_due_date("m1", "2025-06-01")
    assert service.milestone_status("m1") == "completed"


def test_change_due_date_on_overdue_returns_pending(service):
    service.add_students([("s1", "c1")])
    service.add_milestones([("m1", "s1", "PSAT", "2026-01-01")])
    service.run_evaluation("2026-02-01")  # 落后
    assert service.milestone_status("m1") == "overdue"
    service.change_due_date("m1", "2026-03-01")
    assert service.milestone_status("m1") == "pending"


def test_missing_milestone_raises(service):
    with pytest.raises(InvalidOperationError):
        service.mark_completed("nope")
    with pytest.raises(InvalidOperationError):
        service.change_due_date("nope", "2026-01-01")


# ---- 升级与通知 ----


def test_escalation_notifies_counselor_and_all_admins(service):
    service.add_students([("s1", "c1")])
    service.add_milestones([("m1", "s1", "PSAT", "2026-01-01")])
    service.run_evaluation("2026-01-31")
    for recipient in ["c1", *ADMINS]:
        esc = [
            n for n in service.list_notifications(recipient)
            if n["kind"] == "escalation"
        ]
        assert len(esc) == 1
        assert esc[0]["eval_date"] == "2026-01-31"
        assert esc[0]["read"] == 0


def test_reescalation_after_leaving_overdue(service):
    service.add_students([("s1", "c1")])
    service.add_milestones([("m1", "s1", "PSAT", "2026-01-01")])
    service.run_evaluation("2026-01-31")  # 第 1 次升级
    service.change_due_date("m1", "2026-02-01")  # 离开落后
    service.run_evaluation("2026-03-02")  # 第 29 天，不算
    assert service.milestone_status("m1") == "pending"
    service.run_evaluation("2026-03-03")  # 新截止日后第 30 天
    assert service.milestone_status("m1") == "overdue"
    esc = [
        n for n in service.list_notifications("c1") if n["kind"] == "escalation"
    ]
    assert len(esc) == 2
    assert "第 2 次升级" in esc[1]["body"]


def test_summary_sent_even_without_escalations(service):
    service.add_students([("s1", "c1")])
    service.add_milestones([("m1", "s1", "PSAT", "2026-01-01")])
    service.run_evaluation("2026-01-10")  # 没有新升级
    c1 = service.list_notifications("c1")
    assert [n["kind"] for n in c1] == ["counselor_summary"]
    assert "新升级 0 项" in c1[0]["body"]
    for admin in ADMINS:
        a = service.list_notifications(admin)
        assert [n["kind"] for n in a] == ["admin_summary"]
        assert "共 0 项" in a[0]["body"]


def test_summary_lists_only_own_students(service):
    service.add_students([("s1", "c1"), ("s2", "c2")])
    service.add_milestones(
        [("m1", "s1", "PSAT", "2026-01-01"), ("m2", "s2", "FAFSA", "2026-01-01")]
    )
    service.run_evaluation("2026-01-31")
    c1 = service.list_notifications("c1")[-1]
    assert c1["kind"] == "counselor_summary"
    assert "s1" in c1["body"] and "s2" not in c1["body"]


# ---- 已读 ----


def test_read_status_per_recipient(service):
    service.add_students([("s1", "c1")])
    service.add_milestones([("m1", "s1", "PSAT", "2026-01-01")])
    service.run_evaluation("2026-01-31")
    mine = service.list_notifications("c1")[0]
    service.mark_read(mine["id"], "c1")
    assert service.list_notifications("c1")[0]["read"] == 1
    # 其他接收人的同内容通知不受影响
    assert service.list_notifications("admin1")[0]["read"] == 0
    # 别人的通知不能被我标读
    other = service.list_notifications("admin1")[0]
    service.mark_read(other["id"], "c1")
    assert service.list_notifications("admin1")[0]["read"] == 0


# ---- 评估日单调性 ----


def test_rerun_completed_date_is_noop(service):
    service.add_students([("s1", "c1")])
    service.add_milestones([("m1", "s1", "PSAT", "2026-01-01")])
    service.run_evaluation("2026-01-31")
    before = service.list_notifications("c1")
    assert service.run_evaluation("2026-01-31") == []
    assert service.list_notifications("c1") == before


def test_earlier_date_rejected(service):
    service.add_students([("s1", "c1")])
    service.add_milestones([("m1", "s1", "PSAT", "2026-01-01")])
    service.run_evaluation("2026-02-01")
    with pytest.raises(EvaluationDateError):
        service.run_evaluation("2026-01-31")


def test_judged_once_per_date(service):
    # 同一评估日重复运行不产生第二次判定/通知
    service.add_students([("s1", "c1")])
    service.add_milestones([("m1", "s1", "PSAT", "2026-01-01")])
    service.run_evaluation("2026-01-31")
    service.run_evaluation("2026-01-31")
    esc = [
        n for n in service.list_notifications("c1") if n["kind"] == "escalation"
    ]
    assert len(esc) == 1
