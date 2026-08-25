# stock_advisor 项目规则

## 产品原则

- 修改伴生研判、LLM、持仓、评测、工作流或 UI 前，必须读取并遵守 `docs/architecture/APP_DEVELOPMENT_PRINCIPLES.md`。它是独立 AI 判断、风险、认知预算、进化和自然对话的 canonical 产品约束。

## 本地边界

- 本项目的运行、修改和定时任务只允许使用 `D:\WILL\STOCK\stock_advisor` 内的本地文件。
- 唯一例外是离线 AI Decision Center 的本机运行目录 `%LOCALAPPDATA%\AIDecisionCenter`：`stock_advisor` 只允许向其 `inbox/` 子目录投递版本化消息文件；Decision Center 只允许在该根目录维护自己的 SQLite、设置、Inbox 和处理归档。
- 上述例外不得扩展到云端、网络服务、其他项目目录或共享数据库；两个本地模块之间唯一的交换 interface 是 Inbox JSON。
- 不访问、不修改、不依赖任何云端任务、云端项目或云端聊天。
- `archive/` 只用于历史溯源，正式运行不得从归档中读取业务规则或状态。

## 定时任务必须使用薄触发

- Codex 中实际安装的自动化 prompt 必须是极薄触发器，只能包含：本地项目根目录、一个 `automations/prompts/triggers/*.md` 入口、入口不可读时停止并报告。
- 自动化 prompt 中禁止保存时点分发、`task_key`、业务规则、文件清单、交易日判断、异常处理、输出格式或写入规则。
- 每个正式业务时点必须对应一个独立 heartbeat、一个独立本地投递 task/thread 和一个唯一 trigger descriptor；禁止一个 heartbeat 或投递 task/thread 承担多个时点。
- 投递 task/thread 只允许校验启动窗口并调用本地 `send_message_to_thread`；禁止读取业务数据、执行分析、写入 ResultStore 或生成业务结论。成功时只能输出一行转交确认，正式回复必须集中在 `automations/thread-map.local.json` 登记的三个汇总 task/thread。
- trigger descriptor 固定唯一 `task_key`、计划时点和汇总 task/thread；公共转交规则写在 `automations/prompts/trigger-handoff.md`；汇总 task/thread 收到的工作入口统一为 `automations/prompts/run-registered-task.md`。
- `task_key` 到 Protocol 的路由写在 `automations/14_SCHEDULE_REGISTRY.md`；业务逻辑写在 `docs/protocols/`。
- heartbeat 的 RRULE 必须直接且只生成正式业务时点，禁止依赖笛卡尔组合产生额外“机制性触发”，再由 prompt 静默过滤。
- trigger descriptor 必须允许计划时点后不足 15 分钟的本地启动延迟；超出容差窗口时只在投递 task/thread 报告调度异常，不发送工作消息，不执行或写入业务数据。

## 修改顺序

新增、删除、改名、迁移或调整定时任务时，必须按以下顺序执行：

1. 修改 `automations/prompts/triggers/` 中对应 trigger descriptor，必要时修改公共转交或工作入口；
2. 如路由或公共边界变化，修改 `automations/14_SCHEDULE_REGISTRY.md`；
3. 如业务行为变化，修改 `docs/protocols/` 或其引用文件；
4. 更新 `automations/thread-map.local.json` 中的本地 task/thread 映射；
5. 最后更新 Codex 本地自动化，保持 prompt 为薄触发；
6. 运行 `scripts/validate_automations.ps1 -CheckInstalled`，确认文件结构与已安装任务一致。

不得把聊天历史或 Memory 当作上述文件的替代来源。新开的项目 task/thread 修改定时任务时同样必须遵守本文件。
