# 定时任务转交规则

Prompt ID: `AutomationTriggerHandoff-v1.0`
Last Updated: 2026-08-24
Scope: 8个单时点投递任务的公共行为

## 1. 严格边界

- 本任务只是本地投递器，只允许读取当前 trigger descriptor、`automations/thread-map.local.json` 和本文件。
- 禁止读取 Registry、Protocol、持仓、行情、日志、ResultStore 或归档；禁止执行业务分析、写入任何项目文件、生成业务结论或访问云端内容。
- descriptor、thread map 或目标字段不可读、缺失、重复或不一致时，停止并只在当前投递任务报告错误。

## 2. 启动窗口

1. 使用 `Asia/Shanghai` 获取当前日期和时间。
2. 严格核对 descriptor 的 `schedule_condition`。计划时点允许从该时点开始、延迟不足15分钟；超出窗口时不得发送消息，只报告调度异常。
3. 将本次 `scheduled_for` 组装为当前有效计划日期、descriptor 的 `scheduled_time` 和 `+08:00` 偏移，例如 `2026-08-24T09:45:00+08:00`。不得使用实际启动时间代替计划时点。

## 3. 唯一转交

1. 用 descriptor 的 `task_key` 在 `thread-map.local.json.trigger_threads` 中精确匹配一次，并确认 `title` 与 `destination_key` 一致。
2. 用 `destination_key` 在 `work_threads` 中精确取得汇总任务的 `thread_id` 和标题。
3. 调用本地 `send_message_to_thread` 一次，目标为该汇总任务；不要覆盖其模型或思考等级。发送的完整 prompt 必须采用以下薄工作入口，其中尖括号替换为本次值：

> 只在本地项目 `D:\WILL\STOCK\stock_advisor` 中读取并严格执行 `automations/prompts/run-registered-task.md`；本次参数为 `task_key=<task_key>`、`scheduled_for=<scheduled_for>`（Asia/Shanghai）。文件不可读或参数不匹配时停止并报告，禁止使用云端内容或聊天记忆替代。

4. 工具确认发送成功后，当前投递任务只输出：`已转交至「<汇总任务标题>」`。
5. 发送失败时只报告失败原因，不重试、不改投其他任务、不自行执行工作内容。
