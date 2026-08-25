# stock_advisor 项目规则

## 产品原则

- 修改伴生研判、LLM、持仓、评测、工作流或 UI 前，必须读取并遵守 `docs/architecture/APP_DEVELOPMENT_PRINCIPLES.md`。它是独立 AI 判断、风险、认知预算、进化和自然对话的 canonical 产品约束。

## 本地边界

- 本项目的运行、修改和定时任务只允许使用 `D:\WILL\STOCK\stock_advisor` 内的本地文件。
- 唯一运行时例外是独立产品目录 `%LOCALAPPDATA%\AITradingCompanion`：它保存用户数据、运行数据库、Exchange、草稿和日志；安装资源位于其 `app/` 子目录并只读。
- 迁移期只允许从 `%LOCALAPPDATA%\AIDecisionCenter` 和项目旧 `data/` 读取并复制历史；旧目录不得作为正式运行输入，也不得被自动删除。
- 上述例外不得扩展到云端、网络服务、其他项目目录或共享数据库；桌面端和本地运行时唯一的交换 interface 是 `%LOCALAPPDATA%\AITradingCompanion\exchange` 的版本化 JSON。
- 不访问、不修改、不依赖任何云端任务、云端项目或云端聊天。
- `archive/` 只用于历史溯源，正式运行不得从归档中读取业务规则或状态。

## 本地调度与运行时

- 正式日程唯一来源是 `resources/schedules/tasks.json`；交易日判定由运行时的本地 XSHG 日历完成。不得重新引入 Codex heartbeat、线程投递或 Inbox 作为业务调度链路。
- 每个正式时点都必须由 `companion_schedule_claim(task_key, scheduled_for)` 幂等保护；服务恢复仅允许 15 分钟补偿，超时必须留下 `missed` 状态，不能补造研究结论。
- `src/runtime/ai_trading_companion/` 是业务状态的唯一写入者；Markdown 是确定性可重建投影，LLM 不得直接编辑事实持仓、记忆或工作流文件。
- 修改日程时先改 `resources/schedules/tasks.json`、再改运行时和测试、最后运行 `scripts/test.ps1` 与发布验证。不得把聊天历史或 Memory 当作日程、合同或产品规则的替代来源。

## Git 提交边界

- `src/` 是产品源码的默认完整提交范围。除 `src/` 外的任何文件都必须逐项证明是构建、运行、安装、迁移、验证或其他用户理解当前产品所必需，不能因为“这次做过”就一并提交。
- `tests/`、`docs/`、`scripts/`、`resources/` 等目录采用最小提交原则：测试只保留可复现关键行为或回归的用例，文档只保留当前共享约束与使用说明，脚本只保留其他用户能直接复用的正式工作流，资源只保留运行时实际读取的文件。
- 个人构思、下一步计划、临时调研、运行产物、调试助手和仅对当前机器有意义的文件必须留在本地，不得提交，也不得为了清理 Git 状态而删除。除非用户明确逐文件授权，尤其不得提交 `docs/AI 做事 效率流.md`、`docs/记忆库.md` 及同类个人计划文档的新增或修改。
- 提交前必须运行 `git diff --cached --name-status`，逐项审核所有非 `src/` 文件；发现无法说明其他用户收益的文件时先取消暂存。禁止使用 `git add .`、`git add -A` 或等价的无差别暂存命令。
