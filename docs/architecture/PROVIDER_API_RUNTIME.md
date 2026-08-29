# 小电脑 Provider Broker 运行时

应用唯一的 LLM 边界是小电脑上的 Provider Broker：`<broker.url>/v1/generate` 与 `<broker.url>/v1/generate/stream`。`broker.url` 位于 `%LOCALAPPDATA%\AITradingCompanion\config\settings.local.json`，默认值是 `http://yosef-server:8817`；它只能是 Broker 根 URL，不能包含路径、认证信息或 Provider 直连参数，也不读取环境变量覆盖。

请求只发送 `prompt`、`intellect`、`effort`、`deadline_ms` 和 `output_token_limit`。局域网接口不使用 Client Token 或 `Authorization`，也绝不发送 `model`。模型、Provider、竞速、升级、用量与成本路由全部由 Broker 负责。

内部业务阶段使用非流式接口；用户可见回复使用 SSE。每个 `delta` 在展示前通过 Secret Guard，文本一旦展示即不可回滚。客户端必须等到 `final`，并校验拼接 delta 与 `output_text` 一致；断流、缺少 final 或不一致会保留已显示前缀、记录独立失败。

结构化任务把冻结 packet、输出约束和 JSON Schema 编入 prompt。Broker 返回后，运行时仍负责本地 JSON/Schema、Secret Guard、H0 隔离、EvidenceGate 和业务资格校验。一次业务调用只请求 Broker 一次；业务阶段自己的重试策略不变。

审计写入每个 `llm_attempt`：实际模型、Broker 上游、请求/完成 intellect、request id、usage、成本估算和允许字段的 attempts。普通 UI 不展示 attempts。历史 SQLite 的旧 Provider 表保留只读，不再创建、读取或写入。

旧 `settings.local.json` 的 `provider` 块会在首次启动原子移除，不备份其中的密钥，并补入默认 `broker.url`；其余设置保持不变。桌面端不提供运行配置或 Provider 质量入口；Broker 管理工作在 [Broker 管理台](http://yosef-server:8817/) 完成。
