"""在子进程中运行评估，并在第 N 个提交点后用 os._exit 模拟被杀。"""
import os
import sys

from escalation.nightly import run_evaluation


def main() -> None:
    db, eval_date, kill_at = sys.argv[1], sys.argv[2], int(sys.argv[3])
    batch_size = int(sys.argv[4]) if len(sys.argv) > 4 else 7
    state = {"n": 0}

    def hook(phase: str, d: str) -> None:
        state["n"] += 1
        if state["n"] >= kill_at:
            os._exit(1)  # 模拟进程被杀：不提交、不清理

    run_evaluation(db, eval_date, batch_size=batch_size, progress_hook=hook)


if __name__ == "__main__":
    main()
