"""崩溃测试的子进程入口。

用法: python3 tests/crash_worker.py <db_path> <eval_date>
环境变量:
  ESCALATION_CRASH_AT       到达该检查点时 os._exit(1)，模拟 kill -9
  ESCALATION_JUDGE_BATCH    判定批大小（用小值制造多批次）
  ESCALATION_SUMMARY_BATCH  汇总批大小
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from escalation import EscalationService  # noqa: E402


def main():
    db_path, eval_date = sys.argv[1], sys.argv[2]
    svc = EscalationService(db_path)
    svc.run_evaluation(eval_date)


if __name__ == "__main__":
    main()
