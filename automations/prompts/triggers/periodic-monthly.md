# A股月度复盘 Trigger

Prompt ID: `AutomationTrigger-PeriodicMonthly-v1.0`
task_key: `periodic.monthly`
task_title: `A股月度复盘`
destination_key: `periodic_review`
scheduled_time: `19:00:00`
schedule_condition: `Asia/Shanghai 每月1日，19:00:00 <= 当前时间 < 19:15:00`

读取并严格执行 `automations/prompts/trigger-handoff.md`。本文件只描述唯一投递任务；禁止自行执行对应业务工作。
