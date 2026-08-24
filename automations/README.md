# 本地自动化

本目录保存 Codex 本地定时任务的可迭代配置。实际安装在 Codex 中的 prompt 只负责打开一个单时点 trigger descriptor；完整转交、路由和业务逻辑保存在本目录，不散落在聊天或自动化配置里。

| 投递任务 | Trigger descriptor | 汇总任务 |
|---|---|---|
| A股 09:00 盘前机会发现 | `prompts/triggers/daily-opportunity-0900.md` | 每日盘前盘后 |
| A股 09:45 异常发现 | `prompts/triggers/daily-execution-0945.md` | 每日盘中操作 |
| A股 10:30 趋势确认 | `prompts/triggers/daily-execution-1030.md` | 每日盘中操作 |
| A股 14:30 操作决策 | `prompts/triggers/daily-execution-1430.md` | 每日盘中操作 |
| A股 15:20 收盘复盘 | `prompts/triggers/daily-review-1520.md` | 每日盘前盘后 |
| A股月度复盘 | `prompts/triggers/periodic-monthly.md` | 月季年复盘 |
| A股季度复盘 | `prompts/triggers/periodic-quarterly.md` | 月季年复盘 |
| A股年度复盘 | `prompts/triggers/periodic-annual.md` | 月季年复盘 |

职责分层：

- `prompts/triggers/`：固定唯一 `task_key`，在计划时点后不足 15 分钟的启动容差内转交工作。
- `prompts/trigger-handoff.md`：读取本地 task/thread 映射并调用 `send_message_to_thread`，不执行业务。
- `prompts/run-registered-task.md`：汇总任务接收 `task_key` 与 `scheduled_for` 后执行 Registry、Protocol 和 ResultStore。
- `thread-map.local.json`：集中保存 8 个投递任务和 3 个汇总任务的本地 ID。
- `automations/14_SCHEDULE_REGISTRY.md`：将 `task_key` 路由到唯一 Protocol 章节，并定义公共运行边界。
- `../docs/protocols/`：业务分析、读写和输出逻辑。
- Codex 自动化 prompt：只引用一个 dispatcher 文件。

每个已安装 heartbeat 只绑定表中的一个投递任务，RRULE 只生成一个正式业务时点。所有业务正文都在对应汇总任务中输出。

修改后运行：

```powershell
& .\scripts\validate_automations.ps1 -CheckInstalled
```
