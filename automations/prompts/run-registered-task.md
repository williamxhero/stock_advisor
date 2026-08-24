# 注册任务统一执行入口

Prompt ID: `AutomationRegisteredTaskRunner-v1.0`
Last Updated: 2026-08-24
Scope: 三个固定汇总任务接收定时转交后的正式工作

## 1. 输入契约

- 当前用户消息必须明确提供唯一 `task_key` 和带 `+08:00` 偏移的 `scheduled_for`。
- 禁止根据当前时间、聊天历史、Memory、旧prompt或归档猜测任务；输入缺失、重复或格式异常时停止并报告。
- 读取 `automations/14_SCHEDULE_REGISTRY.md` 的“定时投递注册”，确认 `task_key` 只出现一次，并读取对应 trigger descriptor。
- 核对 `scheduled_for` 的日期、计划时点和日历条件与 descriptor 完全一致。汇总任务可能因排队晚于15分钟启动；只要收到的 `scheduled_for` 合法，不得用汇总任务的实际启动时间重新判定投递窗口。

## 2. 本地边界

- 只允许使用本地项目根目录 `D:\WILL\STOCK\stock_advisor`，以及 `AGENTS.md` 明确允许的 `%LOCALAPPDATA%\AIDecisionCenter\inbox` 投递 seam。
- 禁止访问或修改云端任务、云端项目和云端聊天。
- `archive/` 只用于历史溯源，本任务不得读取其中的规则或状态。

## 3. 正式执行

1. 确认 Registry ID 以 `ScheduleRegistry-local-` 开头，并用输入的 `task_key` 在业务任务注册表中精确匹配一次。
2. 读取注册表指定的 Protocol 文件与章节，核对完整 Protocol ID；身份、路径、章节或路由不唯一时停止并报告。
3. 严格执行 Registry 公共规则、Protocol 公共规则和指定章节；交易日或周期范围判断由相应 Protocol 负责。
4. 对合法任务严格执行 `automations/15_RESULT_DELIVERY.md`：用输入的 `scheduled_for` 初始化 run，生成唯一完整正文、摘要和payload，再完成 ResultStore 与本地 Inbox 投递。
5. `complete` 成功后，只在当前汇总任务中从 `body_path` 原样输出完整正文；不得追加、删减、重新措辞或把正文发回投递任务。
6. Registry、Protocol、ResultStore 或业务数据异常时，按结果投递规范记录 `failed`；若尚未成功 `prepare`，停止并直接报告，不得伪造已保存或已投递状态。
