下面的聊天，对这个应用的改进，有帮助吗？



这直接关系到我之前的测试是否测了应用真正会走的路径。先看 broker 实际打哪个 path。

读完 transport 代码，你的问题有明确答案，而且它推翻了我之前测试的前提。

**代码里的映射**（`provider_routes.py:30-32`）：

```python
def transport_for_family(family: str) -> str:
    return "responses" if family == "openai" else "chat_completions"
```

所以是**反过来的**：

- **codex (openai) → `/v1/responses`**
- **claude (anthropic) → `/v1/chat/completions`**

而且 `provider_routes.py:150-151` 强制校验 `transport` 必须匹配 family，不允许手工改。

这意味着我之前的测试全部打在 `/v1/chat/completions` 上——**对 claude 是对的，对 7 个 codex endpoint 全是错的路径**。那些"156s / ReadTimeout"是在应用根本不会走的 path 上测出来的。

还有两处差异同样关键：

**1. 应用禁止 native tools**（`provider_broker.py:681`）：

```python
forbidden = {"tools", "tool_choice", "parallel_tool_calls", "web_search", "browser", "computer"}
```

我之前所有测试都带 `tools`——测的是应用永远不会发的请求。

**2. payload 形状不同**（`provider_client.py:117-120`）：

- responses：`{"model", "input", "reasoning": {"effort"}}`
- chat_completions：`{"model", "messages", "reasoning_effort"}`

`reasoning_effort` 是 OpenAI 的字段，发给 claude 模型可能被 400。这是真实风险，得测。

**3. 有一道准入闸**（`provider_broker.py:288`, `_directory_contains`）：`GET /v1/models` 必须**精确列出** route 的 model id，否则该 route 连尝试的机会都没有——直接 `definitive_failure`。

先回答你上一个问题，然后把这次跑出来的结果一起给你——有几个结论推翻了之前的报告。

## Claude 用的是 completions，codex 才用 responses

`provider_routes.py:transport_for_family()` 写死了这个映射，而且不允许手动覆盖：

```python
def transport_for_family(family: str) -> str:
    """Keep the user-facing Codex/Claude choice tied to its wire protocol."""
    return "responses" if family == "openai" else "chat_completions"
```

`provider_routes.py:290-291` 会拒绝任何和 family 不一致的 `transport`。所以：

| family                | 端点                        | payload                               |
| --------------------- | --------------------------- | ------------------------------------- |
| `openai`（codex）     | `POST /v1/responses`        | `{model, input, reasoning:{effort}}`  |
| `anthropic`（claude） | `POST /v1/chat/completions` | `{model, messages, reasoning_effort}` |

方向和直觉是相反的：**claude 走老的 completions，codex 走 responses**。实测印证了这点——三个 claude 端点里有两个在 `/responses` 上直接返回 HTTP 500 "not implemented"。两边都是 `Authorization: Bearer`，app 从不发 `x-api-key`。

## 配置已经生效了，而且被 app 迁移到 schema v4

我用 app 自己的 `normalize_provider()` 跑了一遍线上配置（只读）：14 endpoints / 42 routes，`families` 已经正确分家，倍率已经落在 endpoint 上，`tier_mode: auto`。所以之前挂着的「按新格式生成配置」这件事不用再做了，`gen_provider_config.py` 那些缺陷也就无所谓了。倍率被 app 完整接受：

```
api-top-1 0.02  api-top-2 0.03  api-top-claude 0.05  wawapii-1 0.06
code28-1 0.08   liangrekui-0/claude 0.09  wawapii-2 0.11  liangrekui-1 0.115
liangrekui-2 0.145  code28-2 0.19  code28-claude 0.3  wawapii-claude 0.45
```

## codex 的旧测量结果是错的，全部健康

之前 `FINAL_DIAGNOSIS_REPORT.md` 里那些「156s TTFT / ReadTimeout」是在错误路径上、还带着 `tools` 测出来的（broker 的 `_assert_tool_free_payload` 根本禁止 tools）。按 app 真实的 `/responses` 重测，9 个 codex 端点 **全部 3/3 通过**，中位 TTFT 1.5–2.4s：

```
api-top-1 2.381s   code28-1 1.763s   wawapii-1 1.706s   liangrekui-0 2.199s
api-top-2 1.605s(有一次24s)  code28-2 1.730s  wawapii-2 1.954s
liangrekui-1 1.966s  liangrekui-2 1.517s
```

## 两个端点真的是死的

**`api-top-claude`（0.05x，最便宜的 claude）** — `GET /v1/models` 返回 **0 个模型**，所以它的 3 条 route 在任何推理之前就被 admission gate 丢掉了。直连也是 HTTP 503：`No available channel for model claude-sonnet-5 under group CC福利`。渠道侧没货。

**`wawapii-claude`（0.45x，最贵的 claude）** — 0/3，全部 TimeoutError。和之前 5 轮流式只过 1 次一致。

活着的 claude 只有 `code28-claude`（0.3x，3/3，1.804s）和 `liangrekui-claude`（0.09x，3/3，2.134s）。

## 「同台竞技」其实已经在跑了

这是这次最有价值的发现。有三个机制，不是一个：

**1. M1 判断阶段是强制双跑。** `__main__.py:354` 把 `m1_judgment` 设成 `duel`，`_duel()` 用 ThreadPoolExecutor 同时跑 openai 和 anthropic 两条腿，各自独立走自己的「最便宜优先」阶梯。两边结论一致 → 取更便宜的那个，另一个记作 `cross_confirmation_route`；实质冲突 → 再拉第三条 route 做 arbitration 仲裁。这就是真正的同台竞技，而且**不牺牲成本**，因为每条腿都是自己家族里最便宜的先上。目前 anthropic 有 4 条 duel-capable 判断 route，duel 不会因为缺家族而失败。

**2. `near_cost_tolerance: 0.15` 让 claude 在同一档里真能赢。** 同 tier 内先按成本分「近价带」（15% 以内算同一带），带内**质量优先、成本只做次级 tiebreak**。所以已经出现了这些混合带：

```
judgment tier 0  band3:  wawapii-1(codex 0.0600 q100)  vs  api-top-claude(claude 0.0625 q100)
judgment tier100 band2:  wawapii-2(0.1100) / liangrekui-claude(0.1125) / liangrekui-1(0.1150)
research tier200 band1:  liangrekui-claude(0.2045 q95) 赢过 code28-2(0.1900 q88)  ← 质量压过价格
```

**3. 跨家族 hedge 存在，但从不触发。** `_race_tier` 的 hedge 是刻意选**不同家族**的：

```python
first_family = next(iter(active.values()))[0]["model_family"]
index = next((i for i, route in enumerate(remaining) if route["model_family"] != first_family), 0)
```

但触发条件是 8 秒无首 token（`hedge_seconds = hedge.first_token_seconds or 8`，你的 hedge 块里没有这个键，所以是 8.0）。所有活着的端点都在 2 秒左右出首 token，所以这条路径实际上永远走不到。research / fast 阶段就是纯「最便宜的单跑」。

## 建议动作

按影响排序：

**把 `api-top-claude` 和 `wawapii-claude` 设为 `enabled: false`。** 前者渠道没货、每次都白跑一次 probe；后者是最贵的 claude 还全超时。关掉后 anthropic 判断覆盖还剩 liangrekui-claude(tier100) + code28-claude(tier200)，duel 照常工作。

**把 `liangrekui-claude-anthropic-research` 的模型从 `claude-opus-4-8` 换成 `claude-sonnet-5`。** 这是让 claude 真正进入 research 竞技场的最便宜办法。auto tier 用的是 `(input + 0.2*output) / 参考模型` × 倍率：opus-4.8 是 `10.0/4.4 × 0.09 = 0.2045` → tier 200；sonnet-5 是 `4.0/4.4 × 0.09 = 0.0818` → **tier 100，而且是 tier 100 research 的第一名**（下一个是 wawapii-2 的 0.11，超出 15% 近价带）。质量从 95 掉到 92，换来的是 claude 在 research 阶段第一次有实际出场机会，而且比现在便宜 60%。

**给 hedge 加 `first_token_seconds: 3`（可选）。** 只有你想让跨家族 hedge 真的能触发时才需要。现在 8 秒的门槛在 2 秒的 TTFT 面前等于关闭。

`FINAL_DIAGNOSIS_REPORT.md` 里的 codex 测量结论和 `first_token_timeout_seconds` 那条建议（这个字段在 runtime 里不存在）都需要作废。要我重写那份报告，还是直接改配置？

诊断脚本都在 `D:\WILL\AGENT\agent\`：[probe_normalizer.py](app://localhost/epitaxy/probe_normalizer.py) 跑 app 真实 normalizer、[analyze_tiers.py](app://localhost/epitaxy/analyze_tiers.py) 复现分档和近价带、[analyze_admission.py](app://localhost/epitaxy/analyze_admission.py) 查两道准入门、[test_live_transport.py](app://localhost/epitaxy/test_live_transport.py) 按真实 transport 测全部端点。都是只读，不打印密钥。