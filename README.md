# 升学里程碑逾期升级模块（escalation）

供升学指导平台以**库**形式调用的夜间逾期升级模块，无接口层。
数据持久化在本地单个 SQLite 文件中，不依赖任何外部服务。

## 功能

- 学生 / 里程碑注册（上层建好数据后交给本模块）
- 辅导员操作：标记完成、撤销完成、修改截止日期（均不会直接产生"落后"）
- 夜间评估：截止日后第 30 个自然日起判为"落后"（第 29 天不算），
  每次进入落后给负责辅导员和全体管理员各发一条升级通知，
  评估结束后给每位辅导员 / 管理员各发一条当日汇总（没有新升级也发）
- 通知按接收人独立已读状态，可查询、可标读
- 崩溃恢复：任务在任意时刻被杀，补跑结果与一次跑完逐条一致，已读不丢；
  上一个评估日没跑完就来了新评估日，先补完旧的（含其汇总）再跑新的；
  已完整跑过的日期重跑是空操作，更早的日期直接拒绝

## 用法

```python
from escalation import EscalationService

svc = EscalationService("escalation.db", admins=["admin1", "admin2"])

# 上层建数据（夜间任务运行期间不会新增）
svc.add_students([("s1", "counselor_a"), ("s2", "counselor_b")])
svc.add_milestones([("m1", "s1", "PSAT报名", "2026-01-15")])

# 辅导员操作（可来自另一个进程，共用同一数据库文件）
svc.mark_completed("m1")
svc.undo_completed("m1")
svc.change_due_date("m1", "2026-02-01")

# 夜间任务
svc.run_evaluation("2026-02-15")   # 接受 date 或 ISO 字符串

# 通知
for n in svc.list_notifications("counselor_a"):
    ...
svc.mark_read(notification_id, "counselor_a")
```

## 设计要点

- **状态机**：里程碑状态为单值列（未完成 / 已完成 / 落后），
  完成与落后结构性互斥；只有夜间评估能把里程碑置为落后。
- **可恢复评估**：评估分"判定 → 辅导员汇总 → 管理员汇总"三个阶段，
  逐批提交；`eval_runs.phase` 记录进度，`eval_judgments` 保证同一评估日
  每个里程碑只判定一次；升级、通知、判定在同一事务内落库，
  汇总用唯一索引 + `INSERT OR IGNORE` 去重，任意时刻被杀都能安全续跑。
- **并发**：WAL 模式 + `busy_timeout`；夜间任务每批提交后短暂让出（默认 5ms，
  `ESCALATION_BATCH_PAUSE_MS` 可调），保证另一进程的辅导员操作 1 秒内完成。
- **升级与通知一一对应**：每次进入落后生成一条升级记录，
  通知挂在升级上（唯一索引），每位接收人恰好一条。

## 测试

```bash
python3 -m pytest tests/            # 全部（含 5 万学生规模性能测试，约 1 分钟）
python3 -m pytest tests/ --ignore=tests/test_perf.py   # 快速
```

覆盖：第 29/30 天边界、状态操作、升级/汇总/已读、评估日单调性、
在不同检查点杀进程后补跑与干净运行逐条一致（含汇总发到一半）、
中断日期在新日期到来时补完且两天汇总各归各、
并发操作下的全部"任何时候"不变式、全区规模性能。
