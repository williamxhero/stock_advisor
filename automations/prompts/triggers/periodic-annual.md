# A股年度复盘 Trigger

Prompt ID: `AutomationTrigger-PeriodicAnnual-v1.0`
task_key: `periodic.annual`
task_title: `A股年度复盘`
destination_key: `periodic_review`
scheduled_time: `20:00:00`
schedule_condition: `Asia/Shanghai 每年1月3日，20:00:00 <= 当前时间 < 20:15:00`

读取并严格执行 `automations/prompts/trigger-handoff.md`。本文件只描述唯一投递任务；禁止自行执行对应业务工作。
