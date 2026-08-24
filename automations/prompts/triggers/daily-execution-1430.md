# A股 14:30 操作决策 Trigger

Prompt ID: `AutomationTrigger-DailyExecution1430-v1.0`
task_key: `daily.execution.1430`
task_title: `A股 14:30 操作决策`
destination_key: `daily_intraday`
scheduled_time: `14:30:00`
schedule_condition: `Asia/Shanghai 周一至周五，14:30:00 <= 当前时间 < 14:45:00`

读取并严格执行 `automations/prompts/trigger-handoff.md`。本文件只描述唯一投递任务；禁止自行执行对应业务工作。
