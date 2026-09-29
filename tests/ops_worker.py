"""并发测试的操作进程：在夜间任务运行期间持续做辅导员操作。

用法: python3 tests/ops_worker.py DB_PATH SENTINEL_PATH N_MILESTONES
循环做随机操作，直到 sentinel 文件出现；结束时打印 MAX_OP_MS 与 OPS。
"""

import os
import random
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from escalation import EscalationService, InvalidOperationError  # noqa: E402


def main():
    db_path, sentinel, n_milestones = sys.argv[1], sys.argv[2], int(sys.argv[3])
    rng = random.Random(20260929)
    svc = EscalationService(db_path)
    print("READY", flush=True)
    max_op_ms = 0.0
    ops = 0
    while not os.path.exists(sentinel):
        mid = f"m{rng.randrange(n_milestones)}"
        action = rng.randrange(3)
        start = time.perf_counter()
        try:
            if action == 0:
                svc.mark_completed(mid)
            elif action == 1:
                svc.undo_completed(mid)
            else:
                month = rng.randrange(1, 7)
                day = rng.randrange(1, 29)
                svc.change_due_date(mid, f"2026-{month:02d}-{day:02d}")
        except InvalidOperationError:
            pass
        max_op_ms = max(max_op_ms, (time.perf_counter() - start) * 1000)
        ops += 1
        time.sleep(rng.random() * 0.002)
    print(f"MAX_OP_MS={max_op_ms:.1f} OPS={ops}")


if __name__ == "__main__":
    main()
