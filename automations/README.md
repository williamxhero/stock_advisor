# 本地自动化

本目录保存 Codex 本地定时任务的可迭代配置。实际安装在 Codex 中的 prompt 只负责打开一个 dispatcher 文件；完整分发逻辑保存在本目录，不散落在聊天或自动化配置里。

| Codex 任务 | Dispatcher | 负责的业务任务 |
|---|---|---|
| 每日盘中操作 | `prompts/daily-intraday.md` | 09:45、10:30、14:30 |
| 每日盘前盘后 | `prompts/daily-open-close.md` | 09:00、15:20 |
| 月季年复盘 | `prompts/periodic-review.md` | 月度、季度、年度 |

职责分层：

- `prompts/`：将本次触发时点解析成唯一 `task_key`。
- `automations/14_SCHEDULE_REGISTRY.md`：将 `task_key` 路由到唯一 Protocol 章节，并定义公共运行边界。
- `../docs/protocols/`：业务分析、读写和输出逻辑。
- Codex 自动化 prompt：只引用一个 dispatcher 文件。

修改后运行：

```powershell
& .\scripts\validate_automations.ps1 -CheckInstalled
```
