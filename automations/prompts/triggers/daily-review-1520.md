# A股 15:20 收盘复盘 Trigger

Prompt ID: `AutomationTrigger-DailyReview1520-v1.0`
task_key: `daily.review.1520`
task_title: `A股 15:20 收盘复盘`
destination_key: `daily_open_close`
scheduled_time: `15:20:00`
schedule_condition: `Asia/Shanghai 周一至周五，15:20:00 <= 当前时间 < 15:35:00`

读取并严格执行 `automations/prompts/trigger-handoff.md`。本文件只描述唯一投递任务；禁止自行执行对应业务工作。
