# 动态认知策略 interface

## 模块位置

`CognitiveEffortPolicy` 是任务事实与 Provider Broker 调用之间的深模块。它拥有探索深度选择和候选生成；Broker adapter 只执行不可变决定，不拥有生产默认值，也不选择应用内认知策略。

```text
冻结 Stage Packet + 任务期限 + 证据状态
                    │
                    ▼
          CognitiveEffortPolicy
             select / propose_shadow
                    │ EffortDecision
                    ▼
            Provider Broker adapter
                    │ attempt facts
                    ▼
 EvaluationObservatory → EvolutionGovernance
                    │ approved policy version
                    └───────────────┐
                                    ▼
                          CognitivePolicyExecutor
```

## 公开 interface

```python
class CognitiveEffortPolicy:
    def select(self, request: EffortSelectionRequest) -> EffortDecision: ...
    def propose_shadow(self, decision: EffortDecision) -> EffortCandidate | None: ...
```

调用者提供任务键、阶段、冻结 profile、剩余期限、搜索要求和当前政策版本，不提供目标 effort。`EffortDecision` 冻结 intellect、effort、政策版本、规则 ID、选择原因、期限护栏和已验证 fallback 链；正式 LLM attempt 必须引用决定 ID。

## 策略与进化

- Intellect 表示认知上限，首版按任务族选择 `standard`、`smart` 或 `expert`；Effort 表示本次探索深度，首版候选集合为 `medium`、`high`、`xhigh`。
- bootstrap policy 只复现迁移时的当前行为，不构成永久代码默认。普通聊天、研究规划和后台任务也必须通过同一 interface。
- 稳定 profile stratum 至少包括 routine、major、evidence_sparse、deadline_tight 和 data_blocked。数据阻塞不能通过提高 effort 修复。
- shadow 只比较一个相邻 effort，保持冻结 packet、intellect、工具、Schema、EvidenceGate 和时点一致，且永不发布。
- EvaluationObservatory 分别比较质量、判断结果、窗口内合格概率、交付时间、稳定性和成本；无综合总分。EvolutionGovernance 是唯一晋升和回滚裁决者，CognitivePolicyExecutor 是唯一策略写入者。
- Broker 不支持、M1 隔离、Schema/数据隔离、EvidenceGate 或 deadline 的硬退化触发回滚建议或自动回滚回执，不能被平均收益抵消。

## 测试 seam

外部测试通过 `select` 和 `propose_shadow` 验证生产选择与候选，不直接测试规则存储或 profile 分类细节。Broker 使用记录请求的本地 adapter；Observatory 和执行器分别以不可变决定、实验评估和执行回执作为合同，不共享可写状态。
