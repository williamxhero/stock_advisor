# 独立交易伙伴 MemoryHub

状态：accepted

AI 交易伙伴的正式用户/AI 消息、判断、结果、复盘、用户事实与纠错由独立 MemoryHub 的不可变 Episode Ledger 拥有。每条记录保留发生时间、获知时间、提交时间、事实权威、来源与修正关系；摘要、词法索引、实体、关系、事件簇和图谱都只是可删除、可重建投影。

stock_advisor 只依赖版本化 MemoryHub interface，通过生产 HTTP adapter 和测试 adapter 访问；不得共享数据库表或内部实现。Runtime 继续拥有当前持仓、成交、日程、任务、终止和命令回执，并继续作为桌面 Exchange 的唯一对端。MemoryHub 不形成交易判断，也不替 AI 决定研究顺序或停止时点。

MarketHub 与 8815 的历史内容按产品保证视为永久只读，MemoryHub 保存稳定引用、内容 hash 与实际获知时间并按需水合；WAG 与普通网页保存当时读取的正文快照。任何外部内容进入模型上下文前必须先取得 MemoryHub 获知回执。

迁移采用 expand-contract：先发布协议、Ledger 与 adapter，再影子迁移和恢复演练，最后删除本地生产记忆旁路。切换后 MemoryHub 核心不可用时应用诚实不可用，不回退到旧 SQLite 记忆。旧数据库仅作为只读迁移/恢复来源保留，不自动删除。

本 ADR supersede ADR 0002 对“本地 Runtime 数据库拥有长期事实”的决定，并补充 ADR 0016；桌面与 Runtime 的 Exchange interface 保持不变。
