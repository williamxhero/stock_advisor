# EvaluationObservatory interface 设计

## 模块位置

```text
Companion runtime / EvidenceGate / JudgmentLifecycle
                 │ 不可变来源事实
                 ▼
      EvaluationObservatory
        ├─ 内部事件投影器
        ├─ 任务周期评测模型
        ├─ 交付联合预测器
        └─ 配对实验评估器
                 │ 不可变快照与建议
                 ▼
       EvolutionGovernance
                 │ 版本化裁决
                 ▼
 ScheduleRegistry / 各策略所有者
```

Seam 位于来源事实写入之后、治理裁决之前。EvaluationObservatory 隐藏事件归一化、乱序处理、输入水位、有效窗口、失败竞争风险、配对可比性、动态证据成熟度和非劣检验。

## 比较过的 interface 形状

| 方案 | 公开形状 | 优点 | 主要问题 | 决定 |
| --- | --- | --- | --- | --- |
| 事件流投影器 | `project`、`capture_prediction`、`compare`、`query` | 审计、重放、乱序和算法版本语义最清楚 | 把水位、投影器和事件拓扑暴露给业务调用方 | 作为内部实现 |
| 任务周期聚合根 | `synchronize`、`evaluate`、`predict_delivery`、`assess_experiment` | 09:45、M0、10:30 和工作归因具有很强领域局部性 | 跨周期预测和多分层实验会迫使聚合根持续膨胀 | 作为内部领域模型 |
| CQRS 窄端口 | 写命令与多个查询端口分离 | 权限边界和测试替身清楚 | 普通调用方必须理解该选哪个端口，返回模型容易碎片化 | 私有适配器采用 |
| 深模块门面 | 五个面向产品能力的方法 | 小 interface，同时隐藏投影和统计复杂性 | 模块内部实现较重，需要严格契约测试 | 公开 interface |

## 公开 interface

下面是合同形状，不要求按此文件直接复制类型名；实现时应复用项目现有值对象和错误约定。

```python
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Sequence

@dataclass(frozen=True)
class EvaluationRequest:
    scope: "EvaluationScope"
    as_of: datetime
    evaluator_version: str
    request_id: str

@dataclass(frozen=True)
class ForecastRequest:
    scope: "DeliveryScope"
    as_of: datetime
    forecaster_version: str
    request_id: str

@dataclass(frozen=True)
class ExperimentRequest:
    experiment_id: str
    as_of: datetime
    evaluation_policy_version: str
    request_id: str

@dataclass(frozen=True)
class SnapshotQuery:
    task_key: str | None = None
    cycle_id: str | None = None
    experiment_id: str | None = None
    kind: Literal["evaluation", "forecast", "experiment"] | None = None
    known_as_of: datetime | None = None

class EvaluationObservatory:
    def evaluate(self, request: EvaluationRequest) -> "EvaluationSnapshot": ...

    def forecast(self, request: ForecastRequest) -> "ForecastSnapshot": ...

    def assess_experiment(
        self, request: ExperimentRequest
    ) -> "ExperimentAssessment": ...

    def get_snapshot(self, snapshot_id: str) -> "ObservatorySnapshot": ...

    def query(self, query: SnapshotQuery) -> Sequence["SnapshotSummary"]: ...
```

`evaluate`、`forecast` 和 `assess_experiment` 是确定性的追加操作。它们不是可覆盖状态更新：同一请求 ID 和同一规范化输入返回原结果；同一请求 ID 对应不同输入时报告幂等冲突。`get_snapshot` 和 `query` 是只读操作。

## 明确不公开的操作

- `record_event`：来源事实由实际动作所有者在自身事务中记录。
- `mark_qualified`：只有 EvidenceGate 或同类确定性资格门拥有资格判定。
- `publish`：正式发布仍由 Companion 运行时拥有。
- `promote`、`rollback`：只有 EvolutionGovernance 可以形成裁决。
- `set_schedule`、`set_timing`、`set_router`：只有对应的版本化策略所有者可以执行裁决。
- 通用 `execute(sql)`、`rebuild_projection`：属于运维或内部实现，不进入产品 interface。

## 内部模型

### 规范来源事实

来源事实至少保存稳定事件 ID、事件类型、领域主体、发生时间、获知或记录时间、来源主键和 schema 版本。事实可来自规范事件表，也可在迁移期通过只读适配器从 `companion_cycle`、`llm_attempt`、artifact、资格记录、后验和 Router shadow 记录投影。无法可靠重建的历史字段必须标记为 `unknown` 或 `legacy_incomplete`，不能猜填。

### 任务周期投影

任务周期投影关联冻结的 `schedule_id`、`schedule_revision`、计划启动、实际启动、阶段尝试、资格判定、首次正式发布、失败、窗口结束、上游复用和后验结果。各阶段独立分类为：

- `qualified_in_window`
- `qualified_after_window`
- `rejected`
- `failed`
- `missed`
- `pending`

只有 `qualified_in_window` 拥有成功的合格交付耗时。09:45 开盘异常发现以 M0 为主要合格交付，用户等待从该周期实际启动开始，M0 窗口于当日 10:30 结束。09:00 的预取或证据复用只进入归因工作量，不改变 09:45 的等待起点。

### 不可变输出

- `EvaluationSnapshot`：并列保存速度、窗口内合格概率、研究质量、判断结果、安全可靠性和工作归因，不生成统一总分。
- `ForecastSnapshot`：同时保存条件合格交付时间区间、窗口内合格概率、失败或拒绝风险、置信度和适用分层。
- `ExperimentAssessment`：保存冻结回放、实时配对影子、动态成熟度、材料性改善和各受保护维度的非劣结论。

每个输出都保存 `as_of`、完整输入水位、纳入和排除原因、算法版本及评测政策版本。后来事实只能产生新输出，以便校准历史预测和复建当时结论。

## 事务与水位

来源组件在自己的业务事务中同时提交领域事实和规范评测事件；Observatory 不参加并改写该事务。快照生成在一个一致读取水位上完成，并以输入指纹和唯一约束保证并发重试只产生一个语义快照。迟到或乱序事实不修改旧快照，只在更高水位的新快照中出现，并保留此前时点不可得的含义。

生产实现可以先采用 SQLite 单调序列作为水位；interface 只暴露不透明的 `input_watermark`，避免锁定为单表自增 ID。未来拆分执行、资格、后验和影子流时，可在内部升级为水位向量而不改变调用方。

## 私有测试缝

- `ExecutionFactReader`
- `QualificationFactReader`
- `OutcomeFactReader`
- `ExperimentEvidenceReader`
- `SnapshotRepository`
- `EvaluationPolicy`
- `Clock`

外部测试只通过五个公开方法验证行为；内部适配器测试负责 SQLite 事务、事件去重和迁移兼容。最低合同必须覆盖 10:30 边界、失败与拒绝进入概率分母、09:00 工作归因、乱序事件、快照不可覆盖、同包实时配对、动态成熟度以及 Observatory 无法修改生产策略。

## 增量落地顺序

1. 以现有表为事实来源，先生成只读 M0 周期投影和不可变快照，不改变交付路径。
2. 在运行时、EvidenceGate、发布和后验事务中补齐规范不可变事件。
3. 上线 09:45/M0/10:30 的评测和联合预测，并用历史与冻结数据校准。
4. 将现有 Router shadow 适配到统一实验评估，但继续隔离生产输出。
5. 将 `effective_m1_reserve` 的隐式统计写入拆开：执行路径只读取获准策略，Observatory 只产出候选证据。
6. 最后接入 EvolutionGovernance、版本化策略执行回执和桌面“评测与进化中心”。

