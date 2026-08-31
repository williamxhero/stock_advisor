# AI交易伙伴

`AITradingCompanion` 是一个仅在本机运行的 A 股伴生研判应用：它先做独立市场研究，再接收你的观点，最后以 M1 / M2 继续判断和复盘。它不接券商、不自动下单，也不依赖 Codex 定时任务或旧的 AI Decision Center Inbox。

## 开发与发布

- 开发启动：`scripts/run-dev.ps1`
- 回归测试：`scripts/test.ps1`
- 发布目录：`scripts/publish.ps1 -Runtime win-x64`
- MemoryHub Linux 包：`scripts/publish-memoryhub.ps1`
- 本机安装和数据迁移：`scripts/install-local.ps1 -EnableStartup`
- 单独迁移旧数据：`scripts/migrate-legacy.ps1`
- 校验安装：`scripts/verify-install.ps1`

安装版位于 `%LOCALAPPDATA%\AITradingCompanion\app`；用户数据、数据库、草稿、窗口状态、运行日志和 Exchange 位于 `%LOCALAPPDATA%\AITradingCompanion`，均不写回安装目录。

首次安装会创建隔离 Python 运行环境，并从 `scripts/requirements-runtime.txt` 安装本地交易日历依赖。LLM 统一通过小电脑的 Provider Broker 调用；Broker 根 URL 仅由 `%LOCALAPPDATA%\AITradingCompanion\config\settings.local.json` 的 `broker.url` 设置（默认 `http://yosef-server:8817`），不使用 CPA、Provider API Key 或本地模型配置。日程、投递、持久化、Markdown 投影和故障恢复均由应用自己的确定性运行时负责。

## 目录

- `src/runtime/ai_trading_companion/`：调度、周期状态机、Exchange、记忆、持仓和 Router。
- `memoryhub/`：独立部署的交易伙伴 MemoryHub、版本化 interface 与不可变 Episode Ledger。
- `src/desktop/AITradingCompanion.Desktop/`：WPF 三栏应用。
- `resources/`：只读合同、日程、协议、知识基线与模板。
- `tests/runtime/`、`tests/desktop/`：运行时与桌面契约测试。
- `docs/architecture/`：产品约束、UI 命名、记忆库和 Router 设计。
- `archive/`：仅用于溯源的旧项目与在线版本材料，正式运行不得读取。

## 本地日程（Asia/Shanghai）

| 周期 | 时点 |
|---|---|
| 盘前机会发现 | 交易日 09:00（08:30 可预取） |
| 盘中研判 | 交易日 09:45、10:30、14:30、15:20 |
| 月度复盘 | 每月 1 日 19:00 |
| 季度复盘 | 1 / 4 / 7 / 10 月 2 日 19:30 |
| 年度复盘 | 1 月 3 日 20:00 |

交易日由本地 XSHG 日历判定；正式时点只允许 15 分钟的服务恢复窗口，超过窗口会记录为明确遗漏而不会伪造研究结果。
