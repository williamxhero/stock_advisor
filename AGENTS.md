# stock_advisor 项目规则

## 本地边界

- 本项目的运行、修改和定时任务只允许使用 `D:\WILL\STOCK\stock_advisor` 内的本地文件。
- 不访问、不修改、不依赖任何云端任务、云端项目或云端聊天。
- `archive/` 只用于历史溯源，正式运行不得从归档中读取业务规则或状态。

## 定时任务必须使用薄触发

- Codex 中实际安装的自动化 prompt 必须是极薄触发器，只能包含：本地项目根目录、一个 `automations/prompts/*.md` 入口、入口不可读时停止并报告。
- 自动化 prompt 中禁止保存时点分发、`task_key`、业务规则、文件清单、交易日判断、异常处理、输出格式或写入规则。
- 时点到 `task_key` 的选择写在 `automations/prompts/`；`task_key` 到 Protocol 的路由写在 `automations/14_SCHEDULE_REGISTRY.md`；业务逻辑写在 `docs/protocols/`。
- 同一聊天的 heartbeat 因 RRULE 组合产生无效时点时，只在对应 dispatcher prompt 文件中定义静默跳过，不把这些判断复制回自动化 prompt。

## 修改顺序

新增、删除、改名、迁移或调整定时任务时，必须按以下顺序执行：

1. 修改 `automations/prompts/` 中对应 dispatcher；
2. 如路由或公共边界变化，修改 `automations/14_SCHEDULE_REGISTRY.md`；
3. 如业务行为变化，修改 `docs/protocols/` 或其引用文件；
4. 最后更新 Codex 本地自动化，保持 prompt 为薄触发；
5. 运行 `scripts/validate_automations.ps1 -CheckInstalled`，确认文件结构与已安装任务一致。

不得把聊天历史或 Memory 当作上述文件的替代来源。新开的项目 task/thread 修改定时任务时同样必须遵守本文件。
