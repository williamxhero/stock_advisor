# Provider Broker 与本地研究运行时

正式 LLM 链路由 `ProviderBroker` 统一执行。每条 route 绑定 endpoint、model、`model_family`、能力、成本和人工偏好；GPT/OpenAI 与 Claude/Anthropic 可在同一阶段参与，业务代码不再选择端点或拼装 HTTP payload。Codex/OpenAI 固定使用 Responses SSE，Claude/Anthropic 固定使用 Chat Completions SSE；所有请求都禁止携带原生 `tools`、工具消息或 Provider 浏览能力。

## 配置与凭据

本机配置位于 `%LOCALAPPDATA%\AITradingCompanion\config\settings.local.json`，且该文件不纳入版本控制。endpoint 直接保存 `api_key`、`weight`、支持家族、归档状态和更新时间；模板只包含空占位符，UI、日志、统计、导出与错误信息均不回显密钥。CPA 是普通、默认禁用的可选 endpoint；没有 enabled route 引用 CPA 时，运行时不会连接或探测 CPA。

route 的 canonical 结构为：

```json
{
  "id": "example-claude",
  "endpoint": "example",
  "model": "claude-opus-5",
  "model_family": "anthropic",
  "enabled": true,
  "cost": { "tier": 100, "mode": "relative", "weight": 1.0 },
  "preference": 0,
  "stages": ["research", "judgment", "fast"],
  "capabilities": ["stream", "json_schema", "race", "duel", "arbitration"]
}
```

模型库存只接受该 endpoint 真实 `/models` 返回。错误、空列表或目录中缺少目标模型时 route 不可用；404/405 记为 `unknown`，但同样不能虚构模型或进入推理。配置刷新分别保存 `available_models`、`model_directory_status` 与更新时间，桌面编辑不得抹掉这些字段。

能力层级与成本 tier 正交：L1=Luna，L2=Terra/Sonnet，L3=Sol/Opus。fast 依次允许 L1→L2→L3，research 允许 L2→L3，judgment/M1 只允许 L3。只有当前请求层级的真实库存候选不可用或耗尽后才升级，并在 outcome/attempt 中保存 `requested_level`、`actual_level`、`upgrade_reason`；绝不降级。每个能力层内部仍严格按 tier、当次预检、预计成本的 15% 近似同价组、阶段能力、精确成本、preference（越大越优先）和历史软分排序，能力和历史不能跨 tier。

## 成本竞技与可见流

Broker 在每次链路入口对全部 enabled Provider 做两轮真实模型目录健康探测；连续两轮超过半数失败或返回空目录时产生 `PROVIDER_OUTAGE` 并停止，404/405 只记为不确定且不放行 route。首个正式 SSE 请求立即启动，八秒未获首 token 时才在同 tier 启动第二条，优先不同模型家族，同时最多两个正式请求；旧 `max_parallel=0` 迁移为 2。单 route 默认 90 秒并服从阶段绝对截止时间；当前层级和成本组候选耗尽后才能继续。

内部阶段由首个通过协议解析、Schema、秘密检测和业务 verifier 的完整结果获胜。取消中的 hedge 以疑似已计费取消单列，不计入普通成功/失败。可见回复在首段安全文本发布前锁定 route；锁定后不能切换、重写或覆盖，后续失败保留已显示前缀并产生独立失败状态。

M1 使用同一冻结公共证据包分别运行一个真实 L3 OpenAI 家族和一个真实 L3 Anthropic 家族。缺少或未通过任一侧都会明确失败，不接受单边结果；双方通过且实质一致时采用较低成本结果并保存交叉确认，实质冲突进入独立仲裁，仲裁失败输出 `model_judgment_conflict`。参与者和仲裁者都不得接收 H0。

## 本地研究与证据冻结

模型只通过严格 JSON Schema 生成研究计划。运行时通过 AWG MCP 本地执行 `web_search` 发现和 `web_read` 正文核验；不把其他数据系统作为旁路。AWG 单次工具调用连续三次失败，或 search/read 无法完成时产生 `AWG_OUTAGE` stage failure 并停止。EvidenceGate 按事实覆盖、时效、来源权威和冲突判断资格；覆盖不足最多产生两轮结构化修复。证据一旦冻结，成文、重试、竞技、duel 和仲裁复用同一 bundle，不重新搜索。

`scripts/provider_awg_smoke.py` 只是正式 runtime smoke 组合器的 CLI 入口；它不实现第二套 Provider/AWG transport，不打开正式数据库、Exchange、日程或 UI。smoke 报告只保存真实模型目录、层级/协议/tier/倍率、TTFT/耗时、usage/成本、升级轨迹、AWG 状态、证据覆盖与 bundle hash，不保存提示词或证据正文。

Playwright 只允许读取和受控下载，不允许上传、提交表单或修改外部状态。小电脑运行时地址仍由本机配置引用，默认 SearXNG 为 `http://yosef-server:8801`；CPA 和爬虫服务均指小电脑服务，不在本机启动替代服务。

## 审计、统计与质量页

正式 attempt 只长期保存 endpoint、route、模型、家族、阶段、tier、请求/实际能力层、升级原因、协议指纹、时间、TTFT、完成耗时、结果分类、usage、成本和 verifier 等技术元数据。探测明细与正式 attempt 分表；明细保留 90 天并在运行时启动时压缩为永久日聚合。提示词、消息、证据、生成正文、请求头、Cookie 和密钥不进入统计、导出或 UI。

质量指标按 endpoint × model × family × stage 聚合，提供 24h、7d、30d 和全部历史窗口：协议/产品成功率、未获首字率、TTFT/完成耗时 P50/P90/P95、错误类别、竞技/获胜/延迟/取消、token、估算/实际成本以及每次产品成功成本。少于 20 个合格样本显示“数据不足”，但保留样本量；疑似已计费取消的成本单列。

桌面 Provider 质量页只通过 authenticated loopback Gateway 读取和导出脱敏 CSV/JSON，不直接读取 SQLite。历史质量在样本不足时保持中性，且永远不能硬停 route、跳过本次探测或跨 tier。

## 隔离 preview

`preview-rerun` 在独立 home/database 内重放指定原 cycle，冻结并封存报告、证据覆盖、Provider attempts、实际模型/家族、tier、TTFT、usage、估算/实际成本和 bundle hash。preview 不 claim 正式日程、不写权威数据库或 Exchange。用户确认后，approval import 只验证并导入该 exact bundle；它不能搜索、浏览、调用 Provider、重写正文或修改 bundle。
