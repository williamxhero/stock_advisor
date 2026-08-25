# LLM Knowledge Runtime：本地可审计持久知识架构研究

状态：建议稿  
研究日期：2026-08-24  
范围：`stock_advisor` 的三个汇总任务、其定时自动化，以及会更新 Markdown / CSV 的知识维护工作。

## 执行摘要

不建议把现有 Markdown 全部替换为“另一种文本数据”，也不建议把向量库当作知识库。成熟系统通常把这几种职责拆开：

1. **权威状态**放在具备事务、约束和并发控制的结构化存储；
2. **发生过什么**放在不可变的事件/命令审计日志；
3. **给人和 LLM 阅读的说明**保留为 Markdown 等可读投影；
4. **语义检索**只是从上述权威内容派生的索引，命中后必须回到原始版本核验；
5. LLM 只提出受 schema 约束的“变更意图”，本地程序负责校验、提交、生成 Markdown/CSV、投递和恢复。

这也直接解决当前的效率问题：一个正式执行任务先用一次 `bootstrap` 工具得到一个紧凑的、已选择好的状态包；完成研究后只调用一次 `commit_changes`。它不再逐层读 trigger、handoff、registry、协议、多个全文文件，也不让模型自行决定怎样落盘。与当前 `automations/15_RESULT_DELIVERY.md` 的 SQLite ResultStore 是同一路径：把确定性控制流和持久化留在程序中，把不可确定的投资研究留给 LLM。

建议的目标不是“无 Markdown”，而是**Markdown 从权威可写数据库变为受控投影和人类可审阅的政策文档**。`02_TRADING_PLAYBOOK.md`、Protocol、治理文档仍是 Git 管理、人工优先的规范；Casebook、Hypotheses、状态表、机会/决策日志等运行知识则逐步转为结构化权威记录，并由渲染器生成兼容的 `.md` / `.csv` 视图。

## 市面主流模式与它们实际解决的问题

| 模式 | 读 / 写模型 | 优点 | 主要限制 | 对本项目的定位 |
|---|---|---|---|---|
| Markdown + Git workspace | 读原文；LLM 直接编辑或产出 diff | 人可读、评审和历史差异极好 | 多文件不具事务；大文件上下文昂贵；并发合并冲突 | 保留给治理、Protocol、Playbook 和报告投影 |
| 结构化事务库 | 按主键和索引读取；校验后事务写入 | 约束、去重、查询、原子提交和恢复 | 需要 schema/migration 与写接口 | 运行知识的权威源 |
| append-only event / command log | 追加事实、命令及处理结果；投影当前状态 | 审计、重放、追溯“为何改变” | 投影和版本演进需要设计 | 每次 LLM 提案、验证、提交的审计骨干 |
| vector / RAG index | embedding 相似度召回片段 | 大量非结构化资料的语义发现 | 召回不是事实、可能近似且会陈旧 | 可选的二级检索，不可写为权威 |
| knowledge graph | 节点、关系、属性和约束查询 | 因果链/主题/股票关系可显式遍历 | 设计成本高，早期容易过度建模 | 先以关系表实现；复杂关系稳定后再升级 |
| agent memory/checkpoint | thread checkpoint + 跨线程 store | 续跑、人工介入、失败恢复 | 会话记忆不等于领域事实库 | 保存执行状态，不替代知识权威源 |
| schema tool call / JSON Patch / command log | LLM 输出少量结构化操作；工具验证并应用 | 小上下文、可验证、可审计、可重试 | Patch 需版本前提与领域校验 | 推荐的唯一机器写接口 |

### 1. Markdown 与 Git：适合规范和审阅，不适合作为并发运行数据库

Git 的对象模型和历史非常适合保存政策、协议和可审阅的长篇论证；它也提供带旧值校验的 ref 更新及可提交/中止的 ref transaction。[`git update-ref`](https://git-scm.com/docs/git-update-ref) 说明这类事务可在锁定所有 ref 后统一提交，否则不做修改。但这不等于“一次 LLM 对多个工作区文件的编辑”是业务原子事务：文件、SQLite、Inbox 和 Git commit 跨越多个存储系统，必须由本地 runtime 处理 staging、失败恢复和对账。

因此应保留 Markdown 的长处，而不是把它当作高频可变状态的唯一源：

- `docs/governance/`、`docs/protocols/`、`docs/strategy/02_TRADING_PLAYBOOK.md`：人工审阅、Git 版本化的规范；LLM 只能提出变更候选，不能直接提升正式规则。
- `reports/`：每次运行的不可变叙述快照，可直接链接 `run_id` 和输入/输出 hash。
- `docs/research/03_CASEBOOK.md`、`04_HYPOTHESES.md` 与状态/日志 CSV：迁移后是由数据库渲染的阅读视图；过渡期可继续保留现有路径，避免打破 Protocol。

### 2. 结构化事务库：权威事实、当前状态和校验的正常落点

SQLite 明确支持 ACID/崩溃后的原子性；其事务文档也明确指出可有多个读事务但同一时刻仅一个写事务。[SQLite transaction](https://www.sqlite.org/lang_transaction.html)；WAL 可让读写并发，但同样只有一个 writer，且只能用于同一主机而非网络文件系统。[SQLite WAL](https://www.sqlite.org/wal.html) 这与本项目“只在本机运行”的边界高度匹配。

行业 agent 框架也是类似分层：LangGraph 的 checkpoint 是按执行步骤保存的 thread state，而跨 thread 的长期信息由独立的 key/value Store 保存；其生产建议使用持久 store，而非内存 store。[LangGraph persistence](https://docs.langchain.com/oss/python/langgraph/persistence) 这说明“执行续跑状态”和“跨任务知识”应分离，但不要求引入该框架。

同一框架的 durable execution 会从 checkpoint 重新执行未完成工作，因此外部写入需要 idempotency key 或存在性检查，而不是依赖“模型这次不会重复调用”。[LangGraph Functional API](https://docs.langchain.com/oss/python/langgraph/functional-api) OpenAI 的结构化 function calling 也只是让模型产生符合 schema 的工具参数；应用代码才是真正执行函数、保存数据并返回工具结果的一方。[OpenAI function calling](https://developers.openai.com/api/docs/guides/function-calling) 因而 schema 是提案边界，不是持久化边界。

### 3. 追加日志 / event sourcing：审计和重放，不是所有查询的替代品

事件化的要点是保留不可变的 `command_received → validation → committed/rejected → projection_rendered` 链。当前 ResultStore 已具有这种雏形：run、完整回复、Outbox 与 Inbox 投递状态可追溯。建议保留每个命令的原始 JSON、提案模型/协议版本、输入 snapshot hash、结果、失败原因和提交 revision；当前状态表则是可重建的 projection。

SQLite 的 WAL 本身也是“先将改动追加到日志、以 commit record 表示提交、之后 checkpoint”的实现例子。[SQLite WAL](https://www.sqlite.org/wal.html) 这不是要求把所有业务表改成 WAL 文件，而是说明 append-first + 可恢复 projection 是成熟的持久化模式。

### 4. Vector/RAG：用于候选召回，不能成为权威写入源

RAG 的原始研究把非参数记忆实现为可检索的 dense vector index，用于为生成提供相关文本。[Lewis et al., RAG](https://arxiv.org/abs/2005.11401) 它解决的是“从大量文本中找可能相关的证据”，不是“保证哪条事实当前有效”。

向量索引不能作为本项目的权威写入源，理由是：

1. embedding 是派生表示，丢失原文精确字段、版本、否定、时间与证据等级；写入 embedding 无法表达 `expected_revision`、唯一键、约束或审批状态。
2. 常用近似索引主动以召回换速度。以 pgvector 为例，HNSW/IVFFlat 是 approximate nearest-neighbor index，加入索引后可能得到不同结果，参数也在速度/召回间取舍。[pgvector 官方 README](https://github.com/pgvector/pgvector)
3. 同一事实更新后，旧 chunk、embedding、metadata 和索引删除需要同步；把 index 当真相会产生“检索到已撤销规则”的静默错误。
4. 金融决策需要证据链、时间点、来源、Protocol 版本与可复核主键；相似度分数不能替代这些字段。

正确关系应为：**canonical record/document revision → chunk → embedding/index**。召回工具只返回 `record_id` / `revision` / `source_hash`；runtime 再从权威库读取该 revision，并过滤 `status=active`、适用范围和 as-of 时间。第一阶段甚至不需要向量库：按股票代码、题材、日期、状态、证据级别、FTS 关键词和最近修订检索通常更精确、更便宜。

### 5. Knowledge graph：在关系查询成为刚需后引入，而不是先建图

图数据模型将实体表示为节点、关系表示为带类型的连接；Neo4j 的官方说明即以 nodes、relationships 和 properties 描述该模型。[Neo4j graph database](https://neo4j.com/docs/getting-started/graph-database/) RDF 也把数据集定义为一组命名图。[W3C RDF datasets](https://www.w3.org/TR/rdf11-datasets/) 图对“事件 → 题材 → 受益路径 → 股票角色 → 证据 → 决策”的多跳解释有价值。

但目前可以先在 SQLite 使用 `knowledge_relation(from_id, relation_type, to_id, valid_from, valid_to, evidence_id)`：它保留图的显式关系、事务和审计，同时避免额外服务。只有当多跳图查询和人工图浏览确实成为高频需求时，再迁移/投影到图数据库；图数据库本身也仍依赖事务保证，例如 Neo4j 的图、索引和 schema 操作都在 ACID transaction 中执行。[Neo4j transaction management](https://neo4j.com/docs/operations-manual/current/database-internals/transaction-management/)

### 6. 结构化工具调用、Patch 和命令日志：让 LLM 写得少，让程序负责正确

JSON Patch 是 IETF 标准的顺序操作数组，包含 `add`、`remove`、`replace`、`move`、`copy`、`test`；其中 `test` 特别适合 optimistic concurrency 的旧值前提。[RFC 6902](https://datatracker.ietf.org/doc/html/rfc6902) 但本项目不应把通用 Patch 直接暴露给模型去修改任意路径；应定义更窄的领域命令，如 `upsert_hypothesis`、`append_case_observation`、`transition_theme_state`、`append_decision_evidence` 和 `propose_playbook_change`。

这与现代 tool 协议一致：MCP tool 可声明 `inputSchema` 和 `outputSchema`，服务端必须返回符合 output schema 的结构化结果，客户端应验证。[MCP Tools specification](https://modelcontextprotocol.io/specification/2025-06-18/server/tools) 因而一次工具调用可返回一个已验证的 Runtime Packet，而不是要求模型连续读 10 个 Markdown 再自行拼接上下文。

## 推荐分层架构

```text
                     Git 管理的规范层（人工优先）
     governance / protocols / playbook / schema / renderer templates
                                  │ version + hash
                                  ▼
heartbeat ──确定性──► automation_runtime bootstrap
                                  │ 只返回本任务最小事实包
                                  ▼
                           LLM 研究与判断
                                  │ structured ChangeSet（不直接写文件）
                                  ▼
                    knowledge_runtime validate_and_commit
                   ┌──────────────┼─────────────────┐
                   ▼              ▼                 ▼
     SQLite canonical state  append-only command log  ResultStore/outbox
                   │              │                 │
                   └─────revision / source hash─────┘
                                  │
                     确定性 renderer / indexer
                   ┌──────────────┼─────────────────┐
                   ▼              ▼                 ▼
        Markdown/CSV 可读投影  reports snapshot  FTS/vector（可选）
```

### 数据归属

| 数据 | 权威位置 | 对外/人类视图 | 谁能写 |
|---|---|---|---|
| 调度、run、结果正文、Outbox | 现有 `data/runtime/stock_advisor.sqlite3` | `body_path` 与 Decision Center inbox | `automation_results.py` / runtime |
| 当前主题、股票、候选、证据、假说、案例、决策事实 | 同一 SQLite 的新业务表（或 schema/attached DB） | 生成的 `data/state/*.csv`、`docs/research/*.md` | `knowledge_runtime` 事务接口 |
| 每次 ChangeSet、校验结果、渲染结果、失败 | `knowledge_command` / `knowledge_event` append-only 表 | 审计查询、可选报告附录 | runtime，禁止修改历史 |
| 原始长文本证据与报告 | content-addressed 文件或 `knowledge_document_revision` | `reports/`、Casebook 视图 | runtime 在提交时固化 |
| Protocol、治理、正式 Playbook | Git workspace Markdown | 原文件 | 人工；LLM 只产生 proposal |
| embedding/chunks/FTS | 可重建的派生表/索引 | 检索工具 | indexer；绝不直接人工/LLM写为真相 |

这里“同一 SQLite”是为了让 `run`、知识 revision、命令日志和 outbox 可以在**单一事务**中提交；是否扩展现有 `automation_results.py` 或新增 `knowledge_runtime.py` 是实现选择。不要让业务任务同时直写 SQLite、直改 CSV 和直改 Markdown。

### 最小接口（建议）

```text
automation_runtime bootstrap(task_key, scheduled_for)
  -> RuntimePacket {run_id, protocol_revision, state_revision,
                    task_context, required_evidence, writable_capabilities}

knowledge_runtime query(selector, as_of_revision)
  -> records with id, revision, provenance, text excerpt

knowledge_runtime validate_and_commit(ChangeSet, idempotency_key)
  -> CommitResult {run_id, knowledge_revision, event_ids,
                   rendered_paths, rejected_operations}

knowledge_runtime render(knowledge_revision)
  -> deterministic Markdown/CSV paths + hashes
```

`ChangeSet` 应是严格 JSON schema，而非完整文件内容。每个操作至少含：`op_id`、领域动作、目标 `record_id`、`expected_revision`（新增为 null）、字段值、`evidence_ids`、`rationale`、`protocol_id/version`、`confidence`、`requires_human_approval`。对关键状态转换再加业务不变量，例如不可把证据等级不够的推断升级为事实、不可修改已封存的决策记录、不可绕过 Playbook 门槛。

如果使用 JSON Patch，也必须限制为 record 内部补丁，并在工具端强制 `test /revision`；不要允许 `/docs/...` 这类文件系统 path。通用 Patch 是 transport 格式，不是领域授权模型。

## 一个任务的推荐运行序列

1. scheduler 直接把独立 heartbeat 投入正式汇总任务（删除“LLM 作为消息投递员”）；scheduler/adapter 传入固定的 `task_key` 和计划时点。
2. LLM 的首个动作是 `bootstrap`。程序校验容差窗口、Registry、Protocol ID/hash、并以一次或少数批量查询选择该任务的最小状态与证据摘要；生成 `run_id` 和 `state_revision`。
3. LLM 研究外部事实（如协议允许）并形成结论和 `ChangeSet`。它若需要更多资料，调用带 selector 的 `query`，而不是递归阅读整个文件树。
4. `validate_and_commit` 在短事务中校验 schema、权限、事实/推断口径、`expected_revision`、idempotency key、引用证据和业务约束；成功时写当前表、不可变 event/command、run result 和 outbox。
5. commit 后 renderer 从该 `knowledge_revision` 生成 Markdown/CSV，记录每个产物 hash；最后才执行 Inbox 原子投递。失败则保留 staging 与可重试命令，不能声称已提交。

这样一次业务 LLM 典型为 `bootstrap → 必要的定向查询（0..n） → validate_and_commit`，而非“读 descriptor → 读 handoff → 发消息 → 另一个 LLM 再读 runner → 读多份文件 → 随意保存”。工具调用数仍会随研究证据而变，但控制面不再消耗 LLM 轮询。

## 并发、事务、版本控制与恢复策略

### 并发

- 所有写由本地 single-writer queue 或 SQLite `BEGIN IMMEDIATE` 串行化；读可使用 WAL snapshot。SQLite WAL 的确允许 reader 与 writer 并行，但只有一个 writer，故不能假设“多个汇总任务可同时安全写同一状态”。[SQLite WAL concurrency](https://www.sqlite.org/wal.html)
- 每个 heartbeat 实例使用 `(task_key, scheduled_for)` 作为 idempotency key；重复投递返回同一 run 或显式的 duplicate result，不新建第二条业务状态。
- 每个 record 使用单调 `revision`。提交命令带 `expected_revision`；不匹配即 `conflict`，由 runtime 返回最新摘录给 LLM 或转人工，绝不 last-write-wins。
- 将单次事务保持短：LLM 和网络检索都在事务外；仅“校验后写 current state + event + run/outbox intent”在事务内。SQLite 文档也提示一个连接在读事务中看的是稳定历史 snapshot，而不是其他连接的中途改动。[SQLite transaction isolation](https://www.sqlite.org/lang_transaction.html)

### 跨 SQLite、文件和 Inbox 的一致性

无法把本地文件系统写入、Git commit 和 Inbox 文件放进 SQLite 的同一个 ACID transaction。故采用 outbox/reconciliation：

1. 事务内写 `commit`、`render_intent`、`outbox_intent`，并固定所有内容 hash；
2. renderer 以临时文件写入、fsync/校验 hash 后原子 rename 到目标投影；
3. dispatcher 从 outbox 原子写 Inbox；
4. 重启时 `reconcile` 重试尚未 materialize/dispatch 的 intent，若目标文件已有同 hash 则视为完成；不同 hash 则报警并停止覆盖。

Git 只记录已成功 materialize 的规范/投影批次；不要把 Git commit 当作业务成功条件，也不要把自动 commit 与交易日运行耦合。可设置人工审阅后的日终/周期 commit，commit message 写入 `knowledge_revision`、run IDs 和 renderer hash。

### 备份与可验证性

- SQLite 使用 `PRAGMA journal_mode=WAL`、短事务、定期 checkpoint；WAL 文件是持久数据库状态的一部分，备份不能只复制 `.sqlite3` 主文件。[SQLite WAL file](https://www.sqlite.org/wal.html)
- 每日冷备使用 SQLite backup API 或在确保一致性的情况下备份主库与 `-wal` / `-shm`；定期演练从备份恢复并重放 event log 到指定 revision。
- schema migration 自身必须带版本、向前 migration、回滚/备份策略；渲染器必须可由干净 checkout + DB snapshot 复现。
- 对输入 packet、证据正文、ChangeSet、输出报告和投影都保存 SHA-256；报告引用 `run_id`、Protocol/Playbook/数据口径版本，符合现有 `13_DATA_SEMANTICS.md` 的历史可审计原则。

## 渐进迁移路径

### Phase 0：先量化现状（不改变业务）

- 给三个汇总任务记录：每 run 的 prompt token、工具调用、读取文件数、耗时、失败/重试、写入文件数和冲突数。
- 列出每个 Markdown/CSV 的“权威性、写者、读取者、更新频率、是否历史不可变、是否需要行级查询”。这个盘点是 schema 的依据，不能凭文件名迁移。

### Phase 1：确定性调度与只读 Runtime Packet

- scheduler 直接指向正式任务；保持“一时点一个 heartbeat”，但删除 LLM 投递任务。
- 实现只读 `bootstrap/query`，仍由 LLM 按现有规则写 Markdown/CSV；生成 packet manifest 和 hash，以验证上下文压缩没有漏掉必读事实。
- 不引入向量库或图数据库。

### Phase 2：先接管追加型记录

- 将 `05_DECISION_LOG.csv`、`12_OPPORTUNITY_LOG.csv` 的新增记录先落为带唯一 ID 的 SQLite append event，再确定性导出 CSV；旧 CSV 作为初始导入和兼容视图。
- 为 `10_THEME_STATE.csv`、`11_STOCK_STATE.csv` 建 current-state table + revision；生成 CSV，禁止 LLM 直接覆盖整个文件。
- 添加 `validate_and_commit`、event/command 表、idempotency 与 outbox recovery；将 ResultStore 和知识更新放在同一提交编排中。

### Phase 3：接管研究知识的投影

- 将 Casebook 与 Hypotheses 拆为结构化元数据（主题、状态、证据、适用期、置信度）加不可变 Markdown body revision；渲染现有两个 `.md` 文件。
- Playbook 维持人工主控：LLM 只能提交 `propose_playbook_change`，生成 review diff 和证据包；人工批准后才更新 Git Markdown 与 policy revision。
- 将月/季/年报告写为不可变 report revision，而不是让后续任务改写旧报告。

### Phase 4：选择性检索增强

- 先启用 SQLite FTS + filters（日期、ticker、theme、状态、证据级别、revision）；做 retrieval evaluation：给固定查询集标注应命中记录，测 recall、错误命中和 token 节省。
- 只有长篇证据库规模/查询显示 keyword 不够时，为**已提交 revision**异步建立 chunk/embedding。检索结果必须回源验证、携带出处和 revision。
- 若“事件—题材—股票—角色—证据”的多跳追问频繁且关系 schema 已稳定，再从关系表导出 graph view；不要先引入 Neo4j。

## 风险与待验证项

| 风险 / 未决问题 | 处理或验证方式 |
|---|---|
| 三个汇总 task 能否被多个 heartbeat 直接复用 | 使用暂停/测试 heartbeat 进行一次真实 App 验证；若不支持，保留 8 个直接执行任务而非恢复 LLM handoff |
| 现有 `automation_results.py` schema 是否能安全扩展 | 先读 migration、prepare/complete 与测试；以备份库做迁移/回放演练，再决定同库或独立 attached DB |
| 当前 Protocol 直接要求写 MD/CSV | 迁移阶段 renderer 保持原路径和格式；先更新 Protocol/验证器，再禁止直写，不能悄然改变合同 |
| 自动渲染误覆盖人工编辑 | 投影目录只由 renderer 写；规范文档只人工写。每次渲染检查 managed header/hash，检测到人工漂移即停止并报告 |
| LLM 生成的 ChangeSet 语义正确但事实错误 | schema 只能保证形状，不能保证投资事实；要求 evidence ID、事实/推断字段、来源等级和 policy validation，关键规则仍需人工审批 |
| SQLite WAL 并发和备份误用 | 单主机、单 writer、短事务；明确备份 WAL sidecar；验证已安装 SQLite 版本与 Windows 锁行为后再开启 WAL |
| 迁移反而增加 token | 对每个 Runtime Packet 设置 token/file budget；只传摘要和主键，需要全文时显式查询；Phase 0/1 用指标证明收益 |
| 向量检索制造“看似相关”的错误上下文 | 先用 FTS/结构化过滤；向量结果必须显示 provenance、as-of revision 和 active status，并在 evaluation 集中检验 |

## 结论

当前问题的关键不是 Markdown 格式本身，而是把**调度、查询选择、写入、审计和渲染**都交给了逐层阅读的 LLM。适合本项目的当代做法是一个本地的事务型 knowledge runtime：Git/Markdown 管规范和可读投影，SQLite 管运行知识与审计，append-only command/event 管追溯，结构化工具调用管 LLM 修改，FTS/vector/graph 都只是按需要构建的派生读取层。

这会让三个汇总任务继续“更新知识库”，但更新是可保存、可审计、可并发控制、可重放的；同时把 LLM 的工作压缩为真正需要它的研究和判断。

## 自由叙事推理与结构化提交：评测证据

### 先给结论：不要把执行接口误当成认知格式

“LLM 必须像人一样读写自然语言，才会保留市场直觉”是一个值得保护的设计直觉，但目前没有可复现的权威评测证明：**把最终提交限制为 JSON / schema 或使用 constrained decoding，会使模型失去股票判断力、创造性或所谓直觉**。反过来，也没有证据表明全结构化输入/输出会提升投资预测。这不是已经有定论的模型能力问题，而是本项目必须以时间冻结的实证来回答的问题。

这里要分清三个互不等价的干预：

1. **把输入资料强行表格化**：可能丢掉叙事中的因果线索、反常细节、语气和案例类比；这是信息选择/压缩问题。
2. **限制模型的可见推理或中间草稿**：可能影响复杂任务的性能；这是推理空间问题。
3. **只限制最终持久化请求的形状**：例如自由写完研究正文后，再提交一个 `ChangeSet`；这是执行 API 问题。它不要求把输入或推理改成 JSON。

本项目应采用第 3 种，而不是将第 1、2 种一并带入。也就是说，模型可以阅读原始新闻、案例正文、旧报告和自由文字备注，先产出完整的自然语言研究结论、反例和新假说；只有在“把哪些状态写入权威库”这一最后一步，才调用受约束的机器接口。结构化是**执行/持久化接口**，不是模型被允许思考的唯一语言。

```text
原始叙事、新闻、案例、反例、市场观察
                 │（自然语言，按需检索）
                 ▼
        LLM 自由研究 / 比较 / 假说生成
                 │（自然语言报告保留为 run artifact）
                 ├──────────────► 不可变 Markdown 报告快照
                 │
                 ▼
  sidecar ChangeSet：仅列明要写入的事实、状态、证据链接和版本前提
                 │（schema 校验、事务、审计）
                 ▼
          SQLite canonical state → Markdown/CSV 可读投影
```

`ChangeSet` 也不应承载全文思维过程；它只引用该 run 的自然语言报告、证据片段和 `hypothesis_id`。因此“保存 LLM 改动”包括两部分：完整叙事作为不可变内容 revision 保存，少量需要被程序查询/去重/并发控制的状态作为结构化 sidecar 保存。两者都可追溯到同一 `run_id` 和输入快照 hash。

### 已有直接证据能说明什么，不能说明什么

| 问题 | 一手证据 | 能支持的结论 | 不能外推的结论 |
|---|---|---|---|
| constrained decoding 是否可靠地产生符合 schema 的输出？ | [JSONSchemaBench](https://arxiv.org/abs/2501.10868) 用 10,000 个真实 JSON Schema 和官方 JSON Schema Test Suite 评估六套 constrained-decoding 实现的合规性、覆盖、效率和生成质量。 | 结构化输出可以被当作工程可靠性问题评测，而不是提示词祈祷。 | 该 benchmark **不测**开放式创造力、隐式推理质量或金融 alpha，故不能被用来断言约束“不会伤害直觉”。 |
| 约束会不会必然降低文本质量？ | [NeuroLogic A*](https://arxiv.org/abs/2112.08726) 在翻译、数据到文本生成等任务中比较多个 lexical constrained-decoding 方法；其结果显示旧方法可能以质量换约束满足，而所提方法可在这些任务同时提高质量和约束满足。 | 约束的影响取决于约束语言、解码算法与任务；“有约束必然变差”并不成立。 | 这是受控生成任务，不是股票研究，也不是“人格化直觉”的测试。 |
| 自由语言推理和结构化动作能否共存并有效？ | [ReAct](https://arxiv.org/abs/2210.03629) 交替生成自然语言 reasoning trace 与 task-specific action；在 HotpotQA、FEVER、ALFWorld、WebShop 上报告优于相应基线，并称 reasoning 帮助规划/例外处理、行动补充外部信息。 | “叙事推理 + 明确动作”是有直接基准支持的组合，而非两者只能二选一。 | ReAct 的 action token 不是数据库事务设计；结果也不能直接转化为交易收益。 |
| 给 agent 保留显式思考空间是否可能改善工具任务？ | Anthropic 的 [think tool / τ-bench 实验](https://www.anthropic.com/engineering/claude-think-tool) 比较无 think、extended thinking、think、think+任务提示；其 Claude 3.7 airline `pass^1` 报告 0.370（baseline）到 0.570（think+prompt），retail 也有较小提升；同文报告 SWE-bench 的隔离增益。 | 在多步、政策重、需要消化 tool result 的任务中，保留专门的自然语言 scratchpad 可能提升可靠性。 | 这是厂商针对特定模型/环境的评测，不是市场预测证据；其结论也不意味着每个简单工具调用都应增加思考步骤。 |
| LLM 的一般预测能力是否已超过人？ | [ForecastBench](https://arxiv.org/abs/2409.19839) 将问题限定为提交时尚无答案的未来事件以避免泄漏；在其抽样比较中，专家预测者胜过顶级 LLM（`p < 0.01`）。 | 不能因为模型会写出令人信服的叙事，就假设它已有稳定的“直觉”；需要严格的真未来评测和校准。 | 这不是中国股票/个股回报 benchmark，也没有比较 Markdown 与 JSON。 |

因此，现有文献提供的是一个很清晰的**架构证据方向**：自由自然语言 reasoning 和严格 tool/action interface 可以互补；它没有提供“结构化会使 LLM 失去股市直觉”的因果证据。特别是 JSONSchemaBench 主要评的是输出合规、覆盖与效率，ReAct/τ-bench 主要评 agent 成功率；没有一项把“同一股票任务、同一模型、同一 as-of 输入”下的自由全文、全结构、混合 sidecar 三种条件作比较。这个空白不能用直觉或厂商 demo 填补。

### 金融与预测的证据：有信号报告，但远不足以证明“直觉”

股票特定的实证并非不存在，但结论必须窄读：

- Lopez-Lira 与 Tang 的 [*Can ChatGPT Forecast Stock Price Movements?*](https://arxiv.org/abs/2304.07619) 报告 ChatGPT 对新闻标题的评分与样本外日收益显著相关，并讨论小盘股和负面新闻等异质性。这是“文本理解可能含有可交易信息”的证据，不是对任意 LLM 工作流、任意市场或今日模型的收益保证。
- Glasserman 与 Lin 的 [*Assessing Look-Ahead Bias in Stock Return Predictions Generated By GPT Sentiment Analysis*](https://arxiv.org/abs/2309.17322) 专门指出训练语料与回测期重叠造成的前视偏差，以及公司名称带来的 distraction；其匿名化实验说明即使没有直接泄漏，文本与模型已有知识的交互也会扭曲回测。它是本项目做 financial eval 时必须冻结 `as_of`、记录模型版本、并做 anonymization/反事实检验的直接理由。
- 一般预测方面，ForecastBench 的真实未来问题设计和“专家优于最强 LLM”的结果提醒我们：自然语言的解释质量、方向准确率和概率校准是不同的量。股票任务尤其不能只看命中率；市场非平稳、交易成本和选择偏差会把微弱信号放大成虚假的回测优势。

结论不是“不要用 LLM 做投资研究”，而是：LLM 可以作为叙事证据综合与假说生成器，但“它是否有你想要的市场经验/直觉”只能通过脱离训练与开发循环的前瞻、成本后、分 regime 评测来判断。结构化 sidecar 在这里反而是帮助，而不是压制：它使每次自然语言判断都能被无歧义地记录、在之后被对账和证伪。

### 本项目应做的可证伪 A/B/C 评测

不要先争论哪一种认知形式“更像人”；在相同任务、输入、模型、预算下同时跑三种可重复条件。每个 trial 先冻结可见世界，再让模型工作；禁止根据后验收益修改 prompt、schema、案例选择或交易门槛。

| 条件 | 输入与推理 | 最终写入 | 该条件真正检验什么 |
|---|---|---|---|
| A：自由全文 | 原始新闻、案例、状态和 Protocol 均以 Markdown/自然语言供读；模型自由叙事并直接提出文件 diff。 | 由人工/adapter 从全文提取后暂存；不作为自动权威写入。 | 最大叙事自由下的研究与假说质量；也量出解析和落盘摩擦。 |
| B：全结构 | 给模型相同事实但尽可能以固定字段/表格提供；要求固定 JSON 输出。 | JSON 校验后提交。 | 强结构化上下文/输出能否提升一致性、成本和可执行性，及其是否遗漏叙事信息。 |
| C：混合（推荐候选） | 原始叙事、案例正文和反例仍可读；工具返回紧凑索引与结构化当前状态；模型保留自由报告/草稿。 | 独立 sidecar `ChangeSet` 只含主键、版本前提、状态转换、证据 ID、概率/置信度和 report 引用。 | 在不压缩推理素材的前提下，持久化接口能否获得可靠性和效率。 |

#### 冻结与防泄漏

1. 以每个 `(task_key, scheduled_for)` 创建不可变 `EvaluationPacket`：记录 `as_of`、可见文件/原始新闻/行情、每个内容 hash、检索结果、模型/工具/Prompt/Schema 版本与随机 seed（如 API 支持）。未来数据、后来的 Markdown revision 和已有回测结论不可进入 packet。
2. 使用 walk-forward 设计：开发期只用于锁定 A/B/C、阈值和预算；验证期完全不调参；最终挑战期为未碰过的连续未来区间。按时间 block，而非随机拆分；每个股票、新闻和案例只能进入一个时间侧的训练/调试或评测角色。
3. 对新闻做发布时间、交易时段、时区和可得性审计；允许时加入公司名匿名化/替换以及“未来信息诱饵”测试。后者若改变模型选择，应判为泄漏或脆弱性，而非 alpha。
4. 三条件使用相同模型版本、相同最大 token/工具预算、相同候选集合、相同成交延迟、滑点、手续费、仓位与风控；若 C 多一次 `commit` 工具调用，应把该调用的 token/延迟完整计入。
5. 完整保存自然语言报告、tool trace、ChangeSet、被拒绝的提交和最终执行记录。评分脚本只能读取保存的 packet 和随后才解锁的结果；评分前锁定代码与指标定义。

#### 评分面板（预先注册，而非赛后挑指标）

| 面向 | 预先定义的指标 | 为什么不能省略 |
|---|---|---|
| 概率判断 | Brier score、log loss、reliability diagram、分箱 calibration intercept/slope；方向任务明确基准概率和预测期限。 | “看对方向”不能区分运气与过度自信；概率校准直接影响仓位和止损。 |
| 交易结果 | T+1/T+N 的成本后超额收益、信息比率/Sharpe、最大回撤、胜率、MFE/MAE、持仓期、换手、容量代理和交易成本敏感性。 | 单个平均收益会隐藏尾部损失、过度交易和不可实现的成交假设。 |
| 研究质量 | 证据准确率/可追溯率（引用是否支持主张、是否为 `as_of` 可见）；人工盲评的反事实质量（何种新事实会推翻结论）与新假说质量（新颖性、可检验性、后续证伪/支持率）。 | 这才直接测量用户所说的“经验式判断”是否真的带来可验证增量，而不是文风更像人。 |
| 执行可靠性 | schema/提交错误率、冲突率、重复写入率、人工修复时间、审计可重放率。 | 该面向是采用 sidecar 的直接收益，不能与预测能力混为一谈。 |
| 效率 | 输入/输出/思考 token、工具调用数、P50/P95 延迟、失败重试与每个有效决策成本。 | 避免以“更自由”或“更严格”掩盖不可接受的运行成本。 |

#### 统计与 regime

- 以日期或事件簇为 block 做 paired comparison，报告效应量、置信区间和逐期分布；不要只报告汇总收益。金融时间序列相关性下，应使用适合依赖样本的 block bootstrap / HAC 标准误，并在评测计划中固定其参数。
- 同时检验 A/B/C 的多项指标、多个期限、行业和交易门槛会产生 data-snooping。预先指定一个主终点（例如成本后 `T+5` 超额收益和 Brier 的联合门槛），其余为探索性；报告所有比较，并对多重检验控制 FDR 或 family-wise error。禁止在失败后挑一个子组宣布胜利。
- 按可预先定义的 regime（大盘风险偏好、波动率、流动性、涨跌停/政策冲击窗口、行业）分层，且要求各 regime 都有最小样本量。若某条件只在单一牛市或单一题材周期有效，应记录为条件性策略，而非通用“直觉”。
- 使用反事实复跑：替换/删除模型报告中最关键的一条证据、替换 ticker 名称、打乱不应相关的背景文本。若结论对无关文本同样敏感，或不能说明何时会被推翻，则把它视为脆弱叙事而非经验。

### 广泛搜索、次日验证与知识沉淀：仍应属于推理层

用户希望每次判断尽可能广地搜索论坛、新闻、财报、情绪、资金与政策，再预判、在次日验证并总结。这是正确的研究工作流；它不要求也不应被“数据库只收结构化字段”限制。搜索到的原文、相互矛盾的声音、没有被采用的反例、以及模型自己的叙事综合，都应作为自然语言证据包和 run report 留存。结构化 sidecar 只记录：这次覆盖了哪些 source family、哪些证据被采纳/拒绝、形成了什么可检验预测、何时应结算、以及如何链接回原文快照。

ReAct 的直接实验支持“自然语言 reasoning 与从外部环境取证的 action 交替”这一通用模式，而不是先把所有信息压成固定表格再推理。[ReAct](https://arxiv.org/abs/2210.03629) 在其 QA 和交互决策任务中报告，reasoning trace 用于规划、跟踪和修正，action 用于向外部知识源/环境取信息；它并没有声称该结果已在股票收益上验证。ForecastBench 则说明真正未知未来的预测必须在答案揭晓前锁定，且专家人类仍优于其测试中的顶级 LLM。[ForecastBench](https://arxiv.org/abs/2409.19839) 这两项证据合起来支持“检索—综合—预注册预测—事后评分”的评测方向，但**没有**直接证明“搜得更多论坛/新闻一定产生金融 alpha”。

推荐将每次任务的推理层运行成以下闭环：

```text
deterministic source-coverage plan（确定需要覆盖的来源谱系）
                    │
                    ▼
LLM iterative search / reading / contradiction check / synthesis（可自由叙事）
                    │
                    ▼
as-of EvidenceSnapshot + ForecastCard（锁定当时可见证据与可证伪预测）
                    │
                    ▼
T+1 / T+N outcome label（由确定性价格、公告与事件口径结算）
                    │
                    ▼
LLM reflection report（解释证据、机制、反例和错误；不改写历史预测）
                    │
                    ▼
sidecar ChangeSet（只更新证据状态、假说 revision、案例索引和学习标签）
```

`deterministic source-coverage plan` 不是规定模型只能理性地读表，而是在每个任务启动时固定一个**来源谱系和覆盖清单**，例如：

- 公司一手资料：交易所公告、财报/业绩会、投资者关系材料；
- 宏观/政策一手资料：监管、政府、央行或交易所公告；
- 市场事实：可追溯的行情、成交/资金、行业指数和事件时间戳；
- 新闻与研究：至少两个独立编辑来源，并记录转载/通讯社上游；
- 讨论与情绪：论坛/社媒作为“市场参与者观察”，标为低证据等级，保留原帖、作者/频道、时间与采样方式，绝不因转发量直接升级为事实；
- 历史类比与 Casebook：检索相似但也显式寻找不相似的过往案例。

LLM 可在每类内多轮搜索、提出临时假说和反证问题；runtime 只负责记录已覆盖/缺失/失败的 family、原始 URL 或本地来源 ID、抓取时间、发布时间、内容 hash 和可见截止时间。对每个最终观点，应同时列出“支持证据、最强反证、未知项、会否推翻预测的新事件”。这能保留市场研究里有价值的弱信号和叙事，同时使其在后来可以被检查，而不是被事后成功故事吞没。

“更广地搜”也有确定的风险，故 coverage plan 必须包括停止条件，而不是无限增加上下文：

| 风险 | 为什么广搜会放大它 | 运行时护栏 |
|---|---|---|
| 噪声与重复来源 | 大量新闻/论坛会转载同一最初消息，表面上像多个独立确认。 | 对 URL、正文 hash、通讯社/转载链建立 lineage；按独立上游计数，而非按链接数计数。 |
| 时间泄漏 | 抓取页、修订公告、次日评论或模型已有知识可能把未来塞回“当时判断”。 | 每个证据固定 `published_at`、`retrieved_at`、`as_of`、版本/hash；结算前拒绝之后的 revision，并抽检 company-name anonymization。 |
| 叙事过拟合 | 多读到的材料总能为已选结论找到故事，尤其在事后复盘。 | 在结果揭晓前写 `ForecastCard`：方向/概率、期限、触发条件、反证和失效条件；reflection 只追加，不能修改 card。 |
| 上下文淹没与选择偏差 | token 预算迫使模型挑材料，热门题材会获得更多搜索机会。 | 固定每 family 的最低覆盖与最大预算；新来源只有在带来新上游、反证或新变量时才能进入；报告未读的缺口。 |
| 无限搜索 | 继续检索会延迟决策而未必增加独立信息。 | 停止于 coverage 满足、连续若干轮没有新增独立主张/反证、或到达 token/时间预算；把未解决问题显式降置信度。 |

次日/后续验证应由程序先产生不可变 `OutcomeLabel`（实现收益、相对基准、MFE/MAE、是否触及预先写下的触发/失效条件、随后公告/政策事件），LLM 再阅读它和原 `ForecastCard` 写 reflection。这样“总结”能形成 Casebook 的叙事经验，但不会回写或粉饰昨日的预测。真正要学习的不是“这次赚/亏的故事”，而是跨样本的条件命中率、校准、重复错误模式和在哪些 regime 下某类搜证反而产生噪声。

**决策规则建议：**只有 C 在最终挑战期中未劣于 A 的研究/交易主终点，并显著降低提交错误或成本时，才把“自由叙事 + structured sidecar”升级为默认。若 A 在严格、盲化、成本后的挑战期稳定优于 C，保留 A 的自由研究流程，但仍可让人或一个确定性 extractor 将其转为待审 ChangeSet；如果 B 优于两者，也应检查是不是 B 得到了额外事实而非结构本身的收益。结论应服从这个测试，而不是预设结构化或非结构化必然更聪明。
