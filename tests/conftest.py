import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from escalation import EscalationService  # noqa: E402

ADMINS = ["admin1", "admin2"]


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "test.db")


@pytest.fixture
def service(db_path):
    return EscalationService(db_path, admins=ADMINS)


def make_service(db_path, n_students=3, n_milestones=2, due_date="2026-01-01"):
    """构造一个小数据集：每名学生 n_milestones 个里程碑，辅导员每 10 人一个。"""
    svc = EscalationService(db_path, admins=ADMINS)
    svc.add_students(
        (f"s{i}", f"c{i // 10}") for i in range(n_students)
    )
    svc.add_milestones(
        (f"m{i}_{k}", f"s{i}", f"里程碑{k}", due_date)
        for i in range(n_students)
        for k in range(n_milestones)
    )
    return svc
