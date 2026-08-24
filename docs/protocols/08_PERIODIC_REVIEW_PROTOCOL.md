# Periodic Review Protocol

Protocol ID: `PeriodicReview-v1.2`  
Version: 1.2  
Last Updated: 2026-08-24  
Scope: Monthly Review、Quarterly Review、Annual Review

## Change Log

- v1.0（2026-08-21）：将月度、季度、年度Schedule中的统计、归因、反事实分析、规则维护和文件写入逻辑集中到本文件。
- v1.1（2026-08-21）：纳入全市场机会发现、题材生命周期、股票角色、龙头地位、候选淘汰和弱换强的统计验证。
- v1.2（2026-08-24）：所有有效周期任务写入本地ResultStore并投递AI Decision Center，统一正文、摘要与状态语义。

---

## 1. 公共执行规则

### 1.1 任务定位

- 周期复盘负责统计验证、经验提炼和规则维护，不参与盘中交易决策。
- 目标是识别真正有效、偶然有效、过拟合、失效和具有环境依赖的判断，不是证明过去结论正确。
- 严格区分事实、观察、推断、假说、经验、规则候选和正式规则。

### 1.2 必读文件与事实优先级

每次读取当前最新版：

1. `docs/governance/00_PROJECT_GUIDE.md`；
2. `data/portfolio/01_CURRENT_PORTFOLIO.md`；
3. `docs/strategy/02_TRADING_PLAYBOOK.md`；
4. `docs/research/03_CASEBOOK.md`；
5. `docs/research/04_HYPOTHESES.md`；
6. `data/logs/05_DECISION_LOG.csv`；
7. 本文件；
8. `docs/protocols/09_OPPORTUNITY_DISCOVERY_PROTOCOL.md`；
9. `data/state/10_THEME_STATE.csv`；
10. `data/state/11_STOCK_STATE.csv`；
11. `data/logs/12_OPPORTUNITY_LOG.csv`；
12. `docs/governance/13_DATA_SEMANTICS.md`；
13. 对应周期内已有的月度、季度、年度总结。

事实冲突按：

> 用户最新明确真实成交/资产信息 > `data/portfolio/01_CURRENT_PORTFOLIO.md` > `data/logs/05_DECISION_LOG.csv`正式记录 > Project其他文件 > 历史聊天 > 普通Memory

### 1.3 数据与统计口径

- 按交易日而非自然日补录T+1/T+3/T+5；不得改写当时判断。
- 所有收益率注明价格口径、基准和交易日口径；优先同时计算绝对收益和相对适当基准的超额收益。
- 统计样本量、方向正确率/胜率、盈亏比、平均数、中位数、T+1/T+3/T+5、最大有利变动和最大不利变动；仅在数据支持时计算。
- 按信号、时点、市场环境、行业/题材、市值、Beta、仓位和Daily Execution Protocol版本分组。
- 按事件质量、题材生命周期、股票角色、龙头地位变化、机会来源和Opportunity Discovery Protocol版本分组。
- 统计候选发现率、入选率、拒绝率、假点火率、扩散延续率、拥挤预警准确率、候选T+1/T+3/T+5超额收益、MFE/MAE和弱换强反事实。
- 明确披露缺失值、小样本、选择偏差、幸存者偏差、结果偏差、事后拟合和市场环境不均衡。
- 样本不足时不得给出伪精确结论或把相关性写成因果。

### 1.4 重点问题

- 09:45异常、10:30确认、14:30决策各自及其增量价值；
- 10:30是否有效过滤09:45假信号；
- 14:30卖飞、止损过晚、抄底过早、无效交易和执行偏差；
- 二元仓位清仓阈值、Price Reaction Surprise、相对强弱、资金行为、情绪周期、拥挤度、事件驱动；
- 总仓位、单票集中度、高Beta/同题材/同因子暴露、现金与回撤；
- 哪类股票和市场环境适合或不适合现有方法。
- 新股票发现是否真正扩大投资机会，而不是追逐已拥挤热点；
- 持仓加减仓是否遵守与新股票相同的事件—题材—角色—生命周期逻辑；
- 龙头/容量核心/直接受益核心/扩散承接/普通跟随/擦边股票的结果差异；
- 点火、扩散、分歧、重新加速、拥挤、退潮状态识别与状态迁移准确性；
- 技术指标是否曾越权成为独立交易理由，供应商资金标签是否被误写为机构事实。

### 1.5 反事实分析

对重大卖出、清仓、加仓和错失机会，至少比较适用的替代路径：

- 不操作；
- 延迟到下一确认时点或下一交易日；
- 只减仓不清仓；
- 保留观察仓；
- 按预设条件重新买回；
- 采用不同总仓位/单票仓位。

反事实必须使用当时可获得的信息和可执行约束，避免事后诸葛亮。

### 1.6 文件写入边界

- 可按周期原地维护`docs/research/03_CASEBOOK.md`、`docs/research/04_HYPOTHESES.md`、`data/logs/05_DECISION_LOG.csv`；禁止创建同名副本。
- `data/portfolio/01_CURRENT_PORTFOLIO.md`默认禁止修改；只有用户在当前聊天明确报告尚未写入且信息完整的真实成交时才可更新。
- 不得把研究结论、建议或推测写成真实成交。
- 不得改写Decision Log历史样本的`protocol_version`；周期报告记录自身`PeriodicReview-v1.2`，并比较不同Daily Execution与Opportunity Discovery版本。
- `data/state/10_THEME_STATE.csv`和`data/state/11_STOCK_STATE.csv`是当前分析状态；周期复盘可以校正当前状态，但不得删除或改写`data/logs/12_OPPORTUNITY_LOG.csv`中的历史发现、拒绝和当时证据。
- 文件身份不明、版本冲突或写入失败时停止对应写入并报告，不得创建替代副本。

### 1.7 Playbook变更门槛

- `docs/strategy/02_TRADING_PLAYBOOK.md`默认只读。
- 只有证据达到Guide的Level 4，且样本、逻辑、稳健性、跨环境表现和实际交易意义均充分时，才可原地升级、收窄、降级或删除规则。
- 每项变更必须记录：原规则、累计样本数、支持证据、反证、跨环境表现、适用/失效环境、修改原因、新规则和潜在新风险。
- 证据不足时保留或更新Hypothesis，并明确“本周期没有足够证据修改Playbook”。
- 禁止由一次成功、一次止损或一次卖飞推动规则变更。

### 1.8 协议异常

- 本文件缺失、无法读取、版本不明或与Guide/Playbook冲突时，报告异常，不得使用历史Schedule长Prompt代替。
- Guide和Playbook的高层边界/正式规则优先；冲突留待人工修订Protocol。

---

## 2. Monthly Review Protocol

### 2.1 范围

复盘刚结束的上一自然月，汇总当月正式判断、真实成交、到期T+N结果及既往总结中的未结事项。

### 2.2 必做分析

- 按交易日补录可靠取得的T+1/T+3/T+5；缺失留空并解释。
- 统计9:45、10:30、14:30的样本量、方向正确率、超额表现和相互关系。
- 分析加仓、减仓、清仓结果，以及卖飞、止损过晚、成功避损、无效频繁交易。
- 分析新候选的发现、入选、淘汰和弱换强结果，按题材生命周期与股票角色分组。
- 评估总仓位、个股集中度、高Beta/同题材暴露、现金比例和反事实路径。
- 对重大操作比较不操作、晚一天、只减不清、条件买回等方案。

### 2.3 输出与写入

- 原地更新有价值的Casebook、Hypotheses和Decision Log。
- 生成`reports/periodic/07_MONTHLY_SUMMARY_YYYY-MM.md`，顶部记录`PeriodicReview-v1.2`和纳入分析的Daily Execution/Opportunity Discovery版本。
- 优先输出3—5个重要发现、支持/反证假说、异常模式、规则候选、下月观察项和实际修改文件。

---

## 3. Quarterly Review Protocol

### 3.1 范围

复盘刚结束的上一自然季度，读取该季度各月总结及既往季度/年度总结。

### 3.2 必做分析

- 按信号、时点、环境、行业/题材、市值、Beta、仓位和协议版本分组统计。
- 比较三个时点的增量价值，重点检验10:30过滤假信号、14:30卖飞、二元仓位清仓阈值、Price Reaction Surprise、相对强弱与情绪周期。
- 评估正式规则的正负贡献、适用环境和失效迹象。
- 对重大操作做不卖、晚卖、只减不清、条件买回等反事实分析。
- 评估组合集中度、相关性、现金与回撤。
- 比较持仓核心化程度、普通跟随股滞留时间、弱换强成功率、候选池选择偏差和生命周期状态识别。

### 3.3 输出与写入

- 原地维护Casebook、Hypotheses和Decision Log。
- 生成`reports/periodic/08_QUARTERLY_REVIEW_YYYYQX.md`，顶部记录`PeriodicReview-v1.2`和纳入分析的Daily Execution/Opportunity Discovery版本。
- 输出3—5个核心发现、支持/证伪假说、需升级/降级规则、下一季度验证计划和实际修改文件。

---

## 4. Annual Review Protocol

### 4.1 范围

复盘刚结束的上一自然年度，评估整个决策系统而非提供交易建议；读取全年月度、季度和既往年度总结。

### 4.2 必做分析

- 归因全年收益、回撤和机会成本，区分市场Beta、行业/题材暴露、选股、时点、仓位、执行和规则贡献。
- 系统评估卖飞、止损过晚、抄底过早、仓位集中、同因子暴露和无效交易。
- 对重大交易做不操作、延迟、部分减仓、条件买回等反事实分析。
- 比较资金行为、情绪周期、拥挤度、事件驱动、Price Reaction Surprise、相对强弱等信息源的增量贡献。
- 判断哪些股票类型和市场环境适合或不适合现有方法；评估系统是否需要删减、收窄或重构。
- 评估“龙头视角”相对普通单票管理的年度增量价值：新机会质量、持仓核心化、弱换强、卖飞成本、跟随股损失和现金机会成本。

### 4.3 输出与写入

- 原地维护Casebook、Hypotheses和Decision Log。
- 生成`reports/periodic/09_ANNUAL_REVIEW_YYYY.md`，顶部记录`PeriodicReview-v1.2`和全年使用过的Daily Execution/Opportunity Discovery版本。
- 输出全年最重要发现、规则贡献排名、主要错误模式、系统是否需要重构、下一年度研究重点和实际修改文件。
- 目标是删除无效复杂度，而不是为了完整感增加规则。

---

## 5. 结果保存与投递

- 所有有效周期任务最终必须遵守`automations/15_RESULT_DELIVERY.md`；完整报告正文先保存到ResultStore，再原样作为Codex任务最终回复。
- 报告期尚未结束或按协议无需生成报告时使用`skipped`；协议、数据或文件异常使用`failed`；正常报告使用`succeeded`。
- 月度、季度和年度消息由Decision Center写入历史与通知，不加入每日五节点。
