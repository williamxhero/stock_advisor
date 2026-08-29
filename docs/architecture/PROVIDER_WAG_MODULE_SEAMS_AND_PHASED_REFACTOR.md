# Provider、WAG 与认知阶段模块边界及分阶段重构计划

状态：用户确认的后续开发依据；2026-08-28。

## 1. 目的

Provider 和 WAG 是独立的外部调用模块。M0、M1、M2、研究、成文和聊天只是调用方。外部调用事实与业务内容结果必须分开记录，任何业务阶段都不得把自己的 prompt、Schema、证据资格或 verifier 问题改写成 Provider/WAG 故障。

本计划记录已经确认的事故、模块 interface、状态语义、开发期诊断规则，以及后续分阶段实施与验收顺序。每个阶段通过自己的回归门后再进入下一阶段。

## 2. 已确认的事故与根因

### 2.1 虚假的 26 次 Provider 确定失败

2026-08-28 20:43:32 的一次研究调用在约 0.31 秒内生成 26 条 `definitive_failure`，恰好是 13 个端点的两轮记录，但：

- `attempt_count=0`，没有正式 LLM 请求；
- 记录没有真实开始时间、完成时间和耗时；
- 调用复用了已经过期的阶段绝对截止时间；
- 探测等待窗口立即耗尽，未完成项被本地代码批量填成 `definitive_failure`；
- 两秒后使用新截止时间的调用探测到 12 个可用 Provider，并成功完成研究计划。

因此，这 26 条不是 Provider 调用失败，而是本地任务未开始。正确分类应为 `stage_deadline_exhausted` 或 `probe_not_started`，不得参与 Provider 故障率和宕机判断。

### 2.2 Provider 已返回且扣费，却被记为失败

同一轮 M1 中，多数 GPT/Claude 请求获得了完整响应、Token、TTFT、耗时和扣费记录。Provider 调用事实是成功的，但 M1 verifier 把整个约 32 万字符输入包序列化后搜索任意字符串 `h0`。普通网页 URL、标题、excerpt 和公开 artifact 中偶然出现 `h0`，导致所有输出被确定性拒绝。

Broker 把 `business_rejection` 当成换 Provider 的理由，继续遍历路由和成本组，最终把应用自身的 verifier 错误包装成 `provider_exhausted`。这既错误归因，也造成不必要的重复费用。

### 2.3 M1 输入重复放大费用

本次 M1 输入包约 323,927 个 JSON 字符。一个 87,097 字符的冻结证据正文以两个 artifact 重复进入输入，SHA256 相同；部分 evidence excerpt 也重复。它不是所有输出被拒绝的直接原因，但显著放大 Token、延迟、超时和费用。

### 2.4 WAG 调用状态与搜索结果曾被混淆

WAG MCP 的成功响应是双层 JSON。客户端必须分别处理 HTTP、JSON/SSE、JSON-RPC `error`、`result.isError`、内层文本 JSON 和 `results`。`results: []` 表示工具调用成功但没有搜索命中，不表示 WAG 宕机；协议解析失败表示客户端兼容性问题，也不能称为 WAG 宕机。

## 3. 不可违反的原则

1. Provider/WAG 调用事实由对应模块独占判定，业务阶段不得覆盖。
2. 收到完整 Provider 内容即为 Provider 调用成功；内容是否符合期待属于调用方的 prompt 和输出合同。
3. WAG MCP 工具正常完成即为 WAG 调用成功；空搜索、空正文和证据不足是不同的下游结果。
4. 未开始、被取消、阶段截止时间耗尽和库存未知不得伪装成外部调用失败。
5. Provider/WAG 模块不读取 M0、M1、H0、EvidenceGate 或其他业务语义。
6. 业务阶段可以决定是否重新调用，但每次重试必须是显式的新调用；Broker 不因内容不合格而暗中遍历全部 Provider。
7. `/models` 是真实模型库存接口，不是推理成功证明。空目录或缺少目标模型只令相应路由不可选，不自动证明端点宕机；404/405 为库存未知。
8. 外部故障必须有真实发出的调用证据。应用自身产生的错误标签不能作为外部故障证据。
9. 开发期 smoke 是诊断工具，不接入 App 正式运行链，不写正式数据库或 Exchange。
10. API key、WAG token、请求头、提示词正文、证据正文和私人消息不得进入诊断报告、统计或 UI。

## 4. 模块及 interface

### 4.1 Provider 管理模块

负责：

- Provider、凭据、URL、倍率、模型家族和路由配置的 CRUD；
- `/models` 库存刷新及其时间、状态和真实模型列表；
- 模型目录、别名、价格和能力元数据；
- 启停、归档、恢复和配置原子保存。

不负责 M0/M1 prompt、业务 Schema、业务 verifier、EvidenceGate 或判断是否可发布。

库存状态至少区分：

```text
available(models)
empty
unknown_http_404
unknown_http_405
probe_transport_failed
probe_not_started
```

只有 `available(models)` 中真实包含目标模型的路由才能调用。其他状态不虚构库存，也不自动升级成 Provider 宕机。

### 4.2 ProviderBroker 模块

外部 interface：

```text
invoke(CallRequest) -> CallOutcome
```

`CallRequest` 只包含调用所需的通用信息：候选路由约束、输入内容、协议参数、effort、流式要求、输出 Token 上限和本次调用截止时间。它不包含业务 verifier。

`CallOutcome` 保存：

- 是否真正开始请求；
- route、endpoint、请求模型、Provider 实际模型和模型家族；
- 完整返回内容或已发布的不可变流式前缀；
- HTTP/协议状态、请求 ID、usage、TTFT、完整耗时和成本；
- 取消、断流、超时和可能已计费状态；
- 所有真实 attempt 轨迹。

Provider 调用状态：

```text
completed
transport_failed
protocol_failed
stream_incomplete
timed_out
cancelled
not_started
```

`completed` 的标准是 Provider 返回了完整内容。JSON 是否符合某个业务 Schema、证据是否合格、判断是否正确均不改变它。

竞技只在调用层竞争：首个完整、安全返回内容的请求获胜。可见流一旦发布首段即锁定路由。内部业务若拒绝内容，由阶段编排模块决定是否带着新的 prompt 发起下一次明确调用。

### 4.3 WAG 模块

外部 interface：

```text
call(tool, arguments) -> WagCallOutcome
```

WAG 模块负责 Bearer 认证、MCP JSON-RPC 请求、普通 JSON/SSE 解析、顶层 `error`、`result.isError`、`result.content[]` 文本提取、内层 JSON 解析、`results` 返回，以及真正可重试调用失败的计数。

WAG 调用状态：

```text
completed_with_results
completed_empty
tool_failed
transport_failed
client_compatibility_failed
not_started
```

`completed_empty` 是成功调用。研究阶段可以换 `categories: general`，仍为空时记录 `no_search_results`，不得产生 `WAG_OUTAGE`。

只有连续三次真实 HTTP、连接、超时或工具错误才允许产生 `WAG_OUTAGE`。客户端格式无法解析必须标记为 `WAG_CLIENT_COMPATIBILITY_ERROR`。

### 4.4 认知阶段编排模块

M0/M1/M2、研究、成文和聊天负责：

- 构造 prompt；
- 定义输出 Schema 和业务输出合同；
- 校验 JSON、Schema、证据引用、H0 隔离和业务资格；
- 决定修复 prompt、重新调用、产生明确失败或发布；
- 保持冻结证据和不可变消息语义。

阶段结果至少区分：

```text
qualified
output_parse_failed
schema_rejected
evidence_insufficient
h0_isolation_rejected
business_verifier_rejected
business_verifier_error
stage_deadline_exhausted
no_eligible_route
external_call_failed
```

这些状态不得写回为 Provider/WAG 调用失败。

H0 隔离必须基于结构化来源：artifact actor、kind、cycle、字段和冻结时点。禁止在整包正文、URL、标题或证据文本中搜索字符串 `h0` 来判定隔离。

## 5. 调用事实、内容结果与统计

一次请求可以同时具有以下事实：

```text
Provider call = completed
Stage output = schema_rejected
Stage final = failed
```

三者必须分别保存。Provider 技术统计只计算真实调用的连接、HTTP、协议、断流、超时、TTFT、完成耗时、Token、成本和取消计费。

业务质量作为独立的下游关联指标展示，包括输出解析率、Schema 接受率、阶段采用率和 EvidenceGate 通过率。下游指标不得改写 Provider 技术成功率，不得作为 Provider 宕机证据，也不得自动跨成本 tier。

WAG 同样分别统计 MCP 调用状态、搜索命中状态、正文取得状态和 EvidenceGate 覆盖状态。

## 6. 开发期外部故障复核规则

`scripts/provider_awg_smoke.py` 只用于开发、测试和排障。任何开发结论准备定性为 `PROVIDER_OUTAGE` 或 `WAG_OUTAGE` 前，必须先用当前配置在独立输出目录运行该 smoke，并保存脱敏报告。

- smoke 通过而 App 失败：先查 App 的截止时间、协议选择、payload、库存过滤、解析、prompt、Schema、verifier 和状态映射，不得归罪外部服务。
- Provider smoke 出现大面积、连续、真实调用失败：停止继续绕行，向用户报告端点、时间、HTTP/连接类别和样本量，由用户修复 Provider。
- WAG 连续三次真实 HTTP、连接、超时或工具错误：停止继续绕行并通知用户。
- WAG 空结果：报告无搜索命中，不报告宕机。
- WAG 格式不兼容：修复客户端并报告兼容性问题，不报告宕机。
- smoke 报告只用于开发诊断，不由 App 自动运行，不进入正式业务调度。

## 7. 分阶段开发计划

### 阶段 0：锁定事故回归

目标：用快速、确定性的测试重现本次错误。

交付与验收：

- 过期截止时间不得生成 13×2 `definitive_failure`；
- 未启动 probe 必须是 `probe_not_started`；
- 普通正文、URL 或标题含 `h0` 不得触发 H0 泄漏；
- Provider 返回完整内容但阶段 verifier 拒绝时，Provider 调用仍为成功；
- WAG `results: []` 不得变成 `WAG_OUTAGE`；
- 新增测试必须在旧实现上失败并精确捕获本次症状。

### 阶段 1：拆分 ProviderBroker 调用 interface

目标：Broker 只负责路由和真实调用事实。

交付：

- 引入 `CallRequest`、`CallOutcome` 和独立 attempt 状态；
- 从 Broker 移除业务 verifier、`product_success` 决策和 `business_rejection` 路由切换；
- 完整内容返回即结束本次竞技；
- 截止时间已耗尽时直接返回 `not_started/stage_deadline_exhausted`；
- 未观察到的 probe 不再伪造为确定失败；
- 保持 Responses/Chat Completions 分流、成本顺序、延迟并发和可见流锁定。

验收：Provider 单元与集成测试全部通过；业务 verifier 无法改变 Provider 调用结果。

### 阶段 2：建立阶段输出处理与 prompt 修复链

目标：把内容资格完整移到认知阶段编排模块。

交付：

- 阶段独立执行 JSON、Schema、证据和业务校验；
- 输出不合格时生成明确阶段状态；
- 是否修复 prompt 或重新调用由阶段策略显式决定；
- 确定性 packet/preflight 错误在付费调用前阻断；
- verifier 抛异常与 verifier 正常拒绝分开；
- H0 隔离改为结构化来源校验。

验收：人为制造错误 prompt 时可以看到 Provider 成功、阶段失败；不会自动耗尽所有 Provider。

### 阶段 3：拆分 WAG 调用与研究结果

目标：WAG 只报告 MCP 调用事实，EvidenceGate 只报告证据资格。

交付：

- 完整实现 JSON/SSE 和双层 JSON 解析；
- 检查 JSON-RPC `error` 与 `result.isError`；
- 空搜索、空正文、兼容性错误和真实 outage 独立分类；
- 三次真实可重试失败才产生 `WAG_OUTAGE`；
- 本地研究只消费 `WagCallOutcome`，不得反写 WAG 状态。

验收：覆盖普通 JSON、SSE、工具错误、空结果、格式错误和三次真实故障。

### 阶段 4：拆分审计和质量统计

目标：技术可靠性和下游业务采用率同时可见但互不污染。

交付：

- 迁移 attempt 状态与统计公式；
- 技术成功率不再受 Schema/verifier 影响；
- 下游接受率使用独立字段和聚合；
- `not_started`、库存未知、空搜索和疑似已计费取消分别统计；
- 历史错误记录保留但标注旧语义，不静默重写审计事实。

验收：用本次事故样本验证统计，12 个完整返回必须计为 Provider 技术成功。

### 阶段 5：压缩并去重阶段输入

目标：不改变冻结证据语义的前提下降低 Token、延迟和费用。

交付：

- 同 SHA256 artifact 只进入一次；
- 重复 excerpt 去重；
- 阶段只携带相关内容；
- bundle/hash 作为冻结身份，正文由阶段组装一次；
- 保存输入组成、字符数和估算 Token 的技术元数据，不保存正文到 Provider 统计。

验收：本次 M1 冻结包语义等价，输入规模显著下降，bundle hash 和引用关系可验证。

### 阶段 6：端到端移植与隔离 preview

目标：在正式 App 接通新的模块 seam，并完成真实链验收。

顺序：

1. 本地模拟 Provider/WAG 测试；
2. 开发期运行独立 Provider + WAG smoke；
3. runtime 与迁移测试；
4. desktop、`scripts/test.ps1`、Release 构建、安装和发布验证；
5. 使用 2026-08-27 15:20 原 cycle 执行隔离 preview；
6. 报告证据覆盖、真实调用状态、Provider、模型、家族、tier、TTFT、成本和 bundle hash；
7. 未经用户确认不导入，确认后只导入冻结 bundle，不重新搜索或调用模型。

验收：App 可以明确区分外部调用失败、内容不合格、证据不足和本地截止时间错误；任何界面或报告都不得再用笼统的“Provider/WAG 失败”覆盖这些事实。

## 8. 暂不实施的内容

- 不把 smoke 工具接入正式运行时；
- 不让 App 在业务失败后自动执行全量 smoke；
- 不用历史统计硬停 Provider；
- 不用空 `/models` 虚构模型；
- 不因 prompt 或 verifier 问题自动遍历所有 Provider；
- 不重新解释或删除已有长期审计记录；
- 不在本计划阶段修改正式数据库、Exchange 或冻结 bundle。

## 9. 每阶段共同完成条件

- 有精确覆盖该阶段错误的回归测试；
- 调用 interface 和错误分类有明确测试；
- 不泄露 API key、WAG token、提示词或证据正文；
- 不修改无关用户文件；
- 运行相关 runtime 测试；
- 阶段结论有可复查的命令和输出；
- 未通过当前阶段验收时不进入下一阶段。
