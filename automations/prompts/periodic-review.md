# 月季年复盘 Dispatcher

Prompt ID: `AutomationPrompt-PeriodicReview-v1.0`  
Last Updated: 2026-08-24  
Scope: 本地聊天“月季年复盘”的时点分发

## 1. 本地边界

- 只允许使用本地项目根目录 `D:\WILL\STOCK\stock_advisor`。
- 禁止访问或修改云端任务、云端项目和云端聊天。
- `archive/` 只用于历史溯源，本任务不得读取其中的规则或状态。

## 2. 选择唯一任务

以 `Asia/Shanghai` 当前日期和 `HH:mm` 选择唯一 `task_key`：

| 触发条件 | task_key |
|---|---|
| 每月 1 日 19:00 | `periodic.monthly` |
| 1、4、7、10 月的 2 日 19:30 | `periodic.quarterly` |
| 每年 1 月 3 日 20:00 | `periodic.annual` |

heartbeat 的组合规则会产生其他日期与时点组合。未精确命中上表时，立即静默结束：不读取业务数据、不写文件、不发送结果。

## 3. 正式执行

1. 读取 `automations/14_SCHEDULE_REGISTRY.md`，确认 `Registry ID` 以 `ScheduleRegistry-local-` 开头。
2. 用上一步选出的唯一 `task_key` 在注册表中精确匹配一次。
3. 严格执行注册表的公共规则、路由到的 Protocol 公共规则与指定章节。
4. Dispatcher、Registry 或 Protocol 不可读、身份不符、路由不唯一时，停止并在本地任务中报告；不得用聊天历史、Memory、旧 prompt 或归档内容替代。
