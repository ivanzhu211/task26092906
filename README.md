# task26092906

# 升学里程碑逾期升级模块

纯 Python + SQLite 的库，供上层升学指导平台直接调用，无接口层、无外部服务。
数据全部存放在本地一个 SQLite 文件中（WAL 模式，支持多进程并发读写）。

## 功能

- 学生 / 里程碑 / 辅导员 / 管理员的数据托管（由上层建数）。
- 辅导员日常操作：标记完成、撤销完成、修改截止日期（都不会直接变为落后；
  落后的里程碑改截止日期后回到未完成）。
- 夜间评估 `run_evaluation`：对评估日 D，未完成且 D 不早于截止日期后第 30 个
  自然日的里程碑变为落后（第 29 天不算）；每次进入落后产生一次升级，向该学生
  的辅导员和每位管理员各发一条升级通知。
- 每个评估日结束后，给每位辅导员发名下当日新升级汇总、给每位管理员发全区
  汇总（没有新升级也发）。
- 通知按接收人维度记录已读状态，可查询、可标已读。

## 崩溃恢复与幂等

- 评估按里程碑 id 分批提交（每批一个 IMMEDIATE 事务），`evaluations` 表记录
  阶段与游标；进程在任意时刻被杀后，重新调用即从断点续跑，已提交的批次不重做。
- 升级通知、汇总均靠唯一索引去重：补跑结果与同样数据一次跑完逐条一致，
  补跑前已标已读的通知保持已读。
- 评估日只能往后走：早于已完成评估日的请求抛 `ValueError`；已完成的评估日
  再运行为 no-op；存在中断的上一评估日时，先补完它（含其汇总）再跑新日期；
  没运行过的日期不补。

## 用法

```python
from escalation import EscalationService, run_evaluation

svc = EscalationService("escalation.db")
svc.initialize(admin_ids=["admin1", "admin2"])   # 初始化管理员名单
svc.add_student("stu001", "coun01")              # 上层建数
svc.add_milestone("ms001", "stu001", "PSAT", "2026-03-01")

svc.mark_complete("ms001")                       # 辅导员日常操作
svc.unmark_complete("ms001")
svc.change_due_date("ms001", "2026-04-01")

run_evaluation("escalation.db", "2026-04-15")    # 夜间任务（可中断续跑）

for n in svc.get_notifications("coun01"):        # 接收人查自己的通知
    svc.mark_notification_read(n.id, "coun01")
```

## 测试

```bash
python3 -m pytest
```

覆盖：第 29/30 天边界判定；在评估与汇总的不同位置杀进程后补跑并与一次跑完
的结果逐条一致（含已读状态保留）；夜间任务与另一进程并发操作后的不变量
（落后里程碑的升级通知对每位接收人恰好一条、汇总与当天升级通知对得上、
单次操作 < 1 秒）；中断评估日在下一评估日补完且两天汇总各归各日期；
5 万学生 × 6 里程碑全量评估的性能（实测约 12 秒，远低于 5 分钟上限）。
