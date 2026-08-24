# A股季度复盘 Trigger

Prompt ID: `AutomationTrigger-PeriodicQuarterly-v1.0`
task_key: `periodic.quarterly`
task_title: `A股季度复盘`
destination_key: `periodic_review`
scheduled_time: `19:30:00`
schedule_condition: `Asia/Shanghai 1、4、7、10月的2日，19:30:00 <= 当前时间 < 19:45:00`

读取并严格执行 `automations/prompts/trigger-handoff.md`。本文件只描述唯一投递任务；禁止自行执行对应业务工作。
