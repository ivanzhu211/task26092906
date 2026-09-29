"""评估规则与辅导员操作的单元测试。"""
import pytest

from escalation import EscalationService, run_evaluation


@pytest.fixture()
def svc(tmp_path):
    db = str(tmp_path / "t.db")
    s = EscalationService(db)
    s.initialize(["admin1", "admin2"])
    s.add_student("stu1", "coun1")
    s.add_milestone("m1", "stu1", "PSAT", "2026-01-01")
    s.add_milestone("m2", "stu1", "SAT", "2026-01-02")
    s.add_milestone("m3", "stu1", "FAFSA", "2026-01-01")
    s.db = db
    return s


def test_day29_not_overdue_day30_overdue(svc):
    # 截止 2026-01-01：第29天=01-30 不算，第30天=01-31 算
    run_evaluation(svc.db, "2026-01-30")
    assert svc.milestone_status("m1") == "pending"
    run_evaluation(svc.db, "2026-01-31")
    assert svc.milestone_status("m1") == "overdue"
    # m2 截止 01-02，01-31 是第29天
    assert svc.milestone_status("m2") == "pending"
    run_evaluation(svc.db, "2026-02-01")
    assert svc.milestone_status("m2") == "overdue"


def test_counselor_ops_never_make_overdue(svc):
    svc.mark_complete("m1")
    assert svc.milestone_status("m1") == "completed"
    svc.unmark_complete("m1")
    assert svc.milestone_status("m1") == "pending"
    svc.change_due_date("m1", "2020-01-01")  # 即使截止日远在过去
    assert svc.milestone_status("m1") == "pending"


def test_only_nightly_makes_overdue(svc):
    svc.change_due_date("m1", "2020-01-01")
    assert svc.milestone_status("m1") == "pending"
    run_evaluation(svc.db, "2026-01-05")
    assert svc.milestone_status("m1") == "overdue"


def test_completed_never_overdue(svc):
    svc.mark_complete("m1")
    run_evaluation(svc.db, "2026-03-01")
    assert svc.milestone_status("m1") == "completed"


def test_overdue_change_due_date_back_to_pending(svc):
    run_evaluation(svc.db, "2026-01-31")
    assert svc.milestone_status("m1") == "overdue"
    svc.change_due_date("m1", "2026-01-10")
    assert svc.milestone_status("m1") == "pending"


def test_reescalation_after_leaving_overdue(svc):
    run_evaluation(svc.db, "2026-01-31")
    assert svc.milestone_status("m1") == "overdue"
    svc.change_due_date("m1", "2026-01-10")
    run_evaluation(svc.db, "2026-02-09")  # 再次满30天
    assert svc.milestone_status("m1") == "overdue"
    # 01-31: m1、m3；02-09: m1 再次、m2
    esc = [n for n in svc.get_notifications("admin1") if n.kind == "escalation"]
    assert len(esc) == 4
    esc_c = [n for n in svc.get_notifications("coun1") if n.kind == "escalation"]
    assert len(esc_c) == 4


def test_escalation_notifies_counselor_and_all_admins(svc):
    run_evaluation(svc.db, "2026-01-31")
    for rec in ("coun1", "admin1", "admin2"):
        ns = [n for n in svc.get_notifications(rec)
              if n.kind == "escalation" and n.eval_date == "2026-01-31"]
        assert len(ns) == 2  # m1(PSAT)、m3(FAFSA)
        bodies = sorted(n.body for n in ns)
        assert "PSAT" in bodies[0] or "PSAT" in bodies[1]
        assert "FAFSA" in bodies[0] or "FAFSA" in bodies[1]


def test_summaries_sent_even_when_zero(svc):
    svc.mark_complete("m1")
    svc.mark_complete("m2")
    svc.mark_complete("m3")
    run_evaluation(svc.db, "2026-03-01")
    cs = [n for n in svc.get_notifications("coun1") if n.kind == "counselor_summary"]
    assert len(cs) == 1 and "新升级 0 项" in cs[0].body
    for a in ("admin1", "admin2"):
        ans = [n for n in svc.get_notifications(a) if n.kind == "admin_summary"]
        assert len(ans) == 1 and "全区新升级 0 项" in ans[0].body


def test_summary_matches_escalations(svc):
    run_evaluation(svc.db, "2026-02-01")  # m1、m2、m3 均逾期
    cs = [n for n in svc.get_notifications("coun1") if n.kind == "counselor_summary"][0]
    assert "新升级 3 项" in cs.body
    assert "PSAT" in cs.body and "FAFSA" in cs.body and "SAT" in cs.body
    ans = [n for n in svc.get_notifications("admin1") if n.kind == "admin_summary"][0]
    assert "全区新升级 3 项" in ans.body


def test_notification_read_per_recipient(svc):
    run_evaluation(svc.db, "2026-01-31")
    n_coun = [n for n in svc.get_notifications("coun1") if n.kind == "escalation"][0]
    svc.mark_notification_read(n_coun.id, "coun1")
    assert [n for n in svc.get_notifications("coun1")
            if n.kind == "escalation"][0].read
    # 同一次升级，管理员的副本仍未读
    n_admin = [n for n in svc.get_notifications("admin1") if n.kind == "escalation"][0]
    assert not n_admin.read
    with pytest.raises(KeyError):
        svc.mark_notification_read(n_admin.id, "coun1")  # 别人的通知


def test_rerun_completed_day_is_noop(svc):
    run_evaluation(svc.db, "2026-01-31")
    before = svc.get_notifications("coun1")
    run_evaluation(svc.db, "2026-01-31")
    assert svc.get_notifications("coun1") == before


def test_reject_earlier_eval_date(svc):
    run_evaluation(svc.db, "2026-02-01")
    with pytest.raises(ValueError):
        run_evaluation(svc.db, "2026-01-15")
