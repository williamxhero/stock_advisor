# Schedule Registry

Registry ID: `ScheduleRegistry-local-v1.2`  
Version: 1.2  
Last Updated: 2026-08-24  
Scope: 本地项目全部正式Codex自动化的统一路由、公共运行边界与结果输出策略

## Change Log

- v1.0（2026-08-24）：建立唯一`task_key`入口；将Schedule中的Protocol路由、交易日处理、异常处理、Library边界和Gmail完整正文投递规则集中到本文件。
- v1.1-local（2026-08-24）：迁移到`D:\WILL\STOCK\stock_advisor`；以本地文件替代ChatGPT Library身份，以Codex本地任务输出替代Gmail投递。
- v1.2-local（2026-08-24）：完成目录分层；新增3个文件化dispatcher prompt；Codex自动化改为只引用dispatcher的极薄触发器。

---

## 1. 定位

本文件是本地项目8个业务任务的唯一注册表。Codex自动化先读取各自的dispatcher prompt，再由dispatcher向本文件传入唯一`task_key`。

本地自动化只负责：

1. 在预定时间触发；
2. 读取一个固定的`automations/prompts/*.md`文件；
3. dispatcher不可读取时停止并报告。

时点分发与`task_key`选择由dispatcher负责；任务路由与公共边界由本Registry负责；业务分析、文件读写和输出格式由Registry路由到的Protocol负责。自动化Prompt不得复制其中任何内容。

### Dispatcher注册

| Codex任务 | Dispatcher文件 | task_key范围 |
|---|---|---|
| 每日盘中操作 | `automations/prompts/daily-intraday.md` | `daily.execution.0945`、`daily.execution.1030`、`daily.execution.1430` |
| 每日盘前盘后 | `automations/prompts/daily-open-close.md` | `daily.opportunity.0900`、`daily.review.1520` |
| 月季年复盘 | `automations/prompts/periodic-review.md` | `periodic.monthly`、`periodic.quarterly`、`periodic.annual` |

---

## 2. 任务注册表

| task_key | task_name | 本地Protocol文件 | Protocol章节 | Protocol ID前缀 | calendar_policy | delivery_policy |
|---|---|---|---|---|---|---|
| `daily.opportunity.0900` | A股 09:00盘前机会发现 | `docs/protocols/09_OPPORTUNITY_DISCOVERY_PROTOCOL.md` | `8. 09:00 Pre-Market Discovery Protocol` | `OpportunityDiscovery-` | `a_share_trading_day` | `codex_task` |
| `daily.execution.0945` | A股 09:45异常发现 | `docs/protocols/07_DAILY_EXECUTION_PROTOCOL.md` | `2. 09:45 Protocol：隔夜信息吸收 + 开盘验证 + 异常发现` | `DailyExecution-` | `a_share_trading_day` | `codex_task` |
| `daily.execution.1030` | A股 10:30趋势确认 | `docs/protocols/07_DAILY_EXECUTION_PROTOCOL.md` | `3. 10:30 Protocol：对照09:45确认趋势` | `DailyExecution-` | `a_share_trading_day` | `codex_task` |
| `daily.execution.1430` | A股 14:30操作决策 | `docs/protocols/07_DAILY_EXECUTION_PROTOCOL.md` | `4. 14:30 Protocol：收盘操作 + 次日与未来路径` | `DailyExecution-` | `a_share_trading_day` | `codex_task` |
| `daily.review.1520` | A股 15:20收盘复盘 | `docs/protocols/07_DAILY_EXECUTION_PROTOCOL.md` | `5. 15:20 Protocol：收盘复盘 + 日终知识维护` | `DailyExecution-` | `a_share_trading_day` | `codex_task` |
| `periodic.monthly` | A股月度复盘 | `docs/protocols/08_PERIODIC_REVIEW_PROTOCOL.md` | `2. Monthly Review Protocol` | `PeriodicReview-` | `calendar_period` | `codex_task` |
| `periodic.quarterly` | A股季度复盘 | `docs/protocols/08_PERIODIC_REVIEW_PROTOCOL.md` | `3. Quarterly Review Protocol` | `PeriodicReview-` | `calendar_period` | `codex_task` |
| `periodic.annual` | A股年度复盘 | `docs/protocols/08_PERIODIC_REVIEW_PROTOCOL.md` | `4. Annual Review Protocol` | `PeriodicReview-` | `calendar_period` | `codex_task` |

注册表中的`task_name`是输出投递所用的标准任务名称，不从历史聊天或Memory猜测。

---

## 3. 统一启动流程

收到`task_key`后必须依次执行：

1. 在上表中精确匹配一次；禁止模糊匹配。没有匹配或出现重复匹配时，报告Registry异常并停止。
2. 仅使用本地项目根目录`D:\WILL\STOCK\stock_advisor`及其文件；`archive/`只用于溯源，不参与运行。
3. 按表中本地文件名读取Protocol当前版本，同时核对文件名和`Protocol ID前缀`。
4. 精确执行注册表指定章节；该章节、Protocol公共规则及其引用的Project文件共同构成完整业务逻辑。
5. Registry不覆盖Protocol业务规则。Protocol与Guide或Trading Playbook冲突时，以Guide和Playbook的高层边界与正式规则为准，并报告冲突。
6. 文件身份不明、Protocol或章节缺失、版本/路径不符、规则冲突无法解决或写入失败时，停止受影响操作并输出异常；不得使用旧Schedule、历史聊天或Memory补全。
7. 对既有项目文件必须原地更新，禁止创建同名副本；Protocol明确要求生成新的月度、季度或年度报告时除外。
8. 不得把建议、候选、条件触发或推测写成真实成交；不得自行暂停、删除、改名或修改Schedule。

---

## 4. 日历策略

### `a_share_trading_day`

- 所有日期和时点使用`Asia/Shanghai`。
- 按对应Protocol核验当天是否为沪深交易所A股交易日。
- 非交易日不执行正式分析、不写Project业务文件，生成简短跳过正文。
- 跳过时仍在本地Codex任务中输出简短说明。

### `calendar_period`

- 按对应Periodic Review Protocol定义的自然月、季度或年度范围执行。
- 数据不足、报告期尚未结束或协议异常时，按Protocol说明范围与缺口，不得虚构结果。

---

## 5. 结果投递策略

### `codex_task`

- 完成本次业务处理与获准的项目文件写入后，在本地Codex任务中输出唯一完整最终正文。
- 保留全部标题、段落、表格、链接、数据质量说明、风险提示和异常说明，不只输出通知或摘要。
- 非交易日跳过、协议异常、数据异常或文件写入异常，也必须输出对应说明。
- 由Codex本地自动化通知用户；不依赖ChatGPT Work聊天或Gmail连接。

---

## 6. Schedule Bootstrap规范

本项目自动化Prompt只允许采用以下结构，其中`<dispatcher-file>`必须是上表登记的一个文件：

> 只在本地项目`D:\WILL\STOCK\stock_advisor`中读取并严格执行`<dispatcher-file>`；文件不可读时停止并报告，禁止使用云端内容或聊天记忆替代。

除固定本地根目录、唯一dispatcher路径及入口失败处理外，自动化Prompt不得保存其他规则。时点、日期、`task_key`、Registry身份校验和静默跳过逻辑都必须留在dispatcher文件中。

---

## 7. 维护与审计

- 新增、删除、重命名、迁移任务或更换Protocol章节时，先更新dispatcher，再更新本Registry并升级版本，最后修改Codex本地自动化引用；自动化prompt始终保持薄触发。
- 修改公共交易日、异常或输出规则时，只修改本文件，不批量复制到自动化Prompt。
- 修改业务分析逻辑时，只修改对应Protocol或其引用文件。
- Registry升级只影响后续运行，不回写历史Decision Log、Opportunity Log或既往报告。
- 每次迁移后运行`scripts/validate_automations.ps1 -CheckInstalled`，并复核：dispatcher身份、Registry唯一性、Protocol身份、章节存在性、自动化时间、时区、启停状态和本地项目归属。
