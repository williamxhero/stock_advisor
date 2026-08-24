# 每日盘中操作 Dispatcher

Prompt ID: `AutomationPrompt-DailyIntraday-v1.0`  
Last Updated: 2026-08-24  
Scope: 本地聊天“每日盘中操作”的时点分发

## 1. 本地边界

- 只允许使用本地项目根目录 `D:\WILL\STOCK\stock_advisor`。
- 禁止访问或修改云端任务、云端项目和云端聊天。
- `archive/` 只用于历史溯源，本任务不得读取其中的规则或状态。

## 2. 选择唯一任务

以 `Asia/Shanghai` 当前日期和 `HH:mm` 选择唯一 `task_key`：

| 触发条件 | task_key |
|---|---|
| 工作日 09:45 | `daily.execution.0945` |
| 工作日 10:30 | `daily.execution.1030` |
| 工作日 14:30 | `daily.execution.1430` |

同一 heartbeat 的组合规则还会产生 09:30、10:45、14:45。命中这些无效组合或其他时点时，立即静默结束：不读取业务数据、不写文件、不发送结果。

## 3. 正式执行

1. 读取 `automations/14_SCHEDULE_REGISTRY.md`，确认 `Registry ID` 以 `ScheduleRegistry-local-` 开头。
2. 用上一步选出的唯一 `task_key` 在注册表中精确匹配一次。
3. 严格执行注册表的公共规则、路由到的 Protocol 公共规则与指定章节。
4. Dispatcher、Registry 或 Protocol 不可读、身份不符、路由不唯一时，停止并在本地任务中报告；不得用聊天历史、Memory、旧 prompt 或归档内容替代。
