# AI交易伙伴运行约束

## 事实与投影

`%LOCALAPPDATA%\AITradingCompanion\data\trading-companion.sqlite3` 是唯一事实源。用户消息、AI 里程碑、证据、判断、持仓交易和工作流提案均先写入该库；`workspace/` 下的 Markdown 和 CSV 是可重建的人类可读投影，不能反向覆盖事实。

事实优先级：用户明确的真实成交 > 已落库成交事件 > 已验证的结果和证据 > AI 判断 > 待验证假说。计划、建议、推测或不完整成交绝不能变成持仓事实。

## 本地运行边界

- 日程唯一来源：`resources/schedules/tasks.json`；交易日由本地 XSHG 日历判定。
- 每个时点按 `(task_key, scheduled_for)` 幂等执行；超过 15 分钟恢复窗口必须明确标为遗漏。
- 桌面端通过 `%LOCALAPPDATA%\AITradingCompanion\exchange` 与运行时交换版本化 JSON；不得使用 Inbox、Codex thread 或聊天记忆传递业务状态。
- LLM 只接收运行时生成的阶段包。它不能浏览本地文件、修改代码、修改日程、授予权限或直接写持仓/知识库。

## 研究与判断

- 09:00 做全量公开信息研究；随后盘中阶段只做增量挖掘，并遵守各阶段的固定时效预算。
- M0 是客观观察；M1 必须对当前 H0 盲测；M2 仅在 H0 已锁定时综合 M0、H0、M1。
- 数据或网络缺失必须自然说明实际影响。公开传播即使真假未明也可影响价格，应记录传播强度与可验证性，而不是机械忽略。
- 数据充分且并非流程异常时，AI 应给出明确判断；无法判断必须说明是何种关键证据缺失。

## 持仓与经验

- 当前持仓投影：`workspace/portfolio/01_CURRENT_PORTFOLIO.md`。
- 状态与日志投影：`workspace/state/`、`workspace/logs/`。
- 研究基线：`resources/knowledge/`；协议：`resources/protocols/`；模板：`resources/templates/`；数据口径：`resources/governance/13_DATA_SEMANTICS.md`。
- 错误观点、已证伪观点和反证均应保留为可检索经验，并清楚标识验证结果；不得用事后叙事覆盖原判断。

## 进化边界

AI 可以提出受限的研究覆盖、问题和验证方式改进；提案必须经过本地规则验证和用户批准。它不得自行修改代码、网络权限、自动化、模型安全策略或事实数据。
