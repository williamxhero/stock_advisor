using System.Globalization;
using System.Text.Json;

namespace AITradingCompanion.Desktop.Services;

public sealed record ObservatoryCard(
    string SnapshotId,
    DateTimeOffset CreatedAt,
    string Title,
    string Summary,
    string Detail);

public sealed record ObservatoryDashboard(
    IReadOnlyList<ObservatoryCard> RuntimeHealth,
    IReadOnlyList<ObservatoryCard> ResearchQuality,
    IReadOnlyList<ObservatoryCard> JudgmentOutcomes,
    IReadOnlyList<ObservatoryCard> EvolutionExperiments)
{
    public IEnumerable<ObservatoryCard> All => RuntimeHealth
        .Concat(ResearchQuality)
        .Concat(JudgmentOutcomes)
        .Concat(EvolutionExperiments);
}

/// <summary>Projects versioned Exchange JSON into factual, read-only desktop cards.</summary>
public static class ObservatoryExchangeProjection
{
    private const string Contract = "evaluation-observatory-snapshot/v1";

    public static ObservatoryDashboard Project(IEnumerable<string> messages)
    {
        var runtime = new Dictionary<string, ObservatoryCard>(StringComparer.Ordinal);
        var quality = new Dictionary<string, ObservatoryCard>(StringComparer.Ordinal);
        var outcomes = new Dictionary<string, ObservatoryCard>(StringComparer.Ordinal);
        var experiments = new Dictionary<string, ObservatoryCard>(StringComparer.Ordinal);
        foreach (var message in messages)
        {
            try
            {
                using var document = JsonDocument.Parse(message);
                var root = document.RootElement;
                if (Text(root, "contract") != Contract || Number(root, "contract_version") != 1) continue;
                if (!root.TryGetProperty("snapshot", out var snapshot) || snapshot.ValueKind != JsonValueKind.Object) continue;
                var id = Text(snapshot, "snapshot_id");
                var kind = Text(snapshot, "snapshot_kind");
                if (string.IsNullOrWhiteSpace(id) || !DateTimeOffset.TryParse(Text(snapshot, "created_at"), out var created)) continue;
                if (kind == "evaluation")
                {
                    runtime[id] = EvaluationCard(snapshot, id, created);
                    if (snapshot.TryGetProperty("research_quality", out var research) && research.ValueKind == JsonValueKind.Object)
                        quality[id] = QualityCard(snapshot, research, id, created);
                    if (snapshot.TryGetProperty("judgment_outcomes", out var judgments) && judgments.ValueKind == JsonValueKind.Array && judgments.GetArrayLength() > 0)
                        outcomes[id] = OutcomeCard(snapshot, judgments, id, created);
                }
                else if (kind == "forecast") runtime[id] = ForecastCard(snapshot, id, created);
                else if (kind == "experiment") experiments[id] = ExperimentCard(snapshot, id, created);
            }
            catch (JsonException) { }
        }
        return new ObservatoryDashboard(
            Latest(runtime), Latest(quality), Latest(outcomes), Latest(experiments));
    }

    private static ObservatoryCard[] Latest(Dictionary<string, ObservatoryCard> values) =>
        values.Values.OrderByDescending(item => item.CreatedAt).ThenBy(item => item.SnapshotId, StringComparer.Ordinal).ToArray();

    private static ObservatoryCard EvaluationCard(JsonElement snapshot, string id, DateTimeOffset created)
    {
        var task = FriendlyTask(Text(snapshot, "task_key"));
        var state = DeliveryState(Text(snapshot, "delivery_state"));
        var planned = FriendlyTime(Text(snapshot, "planned_start_at"));
        var actual = FriendlyTime(Text(snapshot, "actual_start_at"));
        var published = FriendlyTime(Text(snapshot, "qualified_published_at"));
        return new ObservatoryCard(id, created, $"{task} · {state}",
            $"计划 {planned}　实际 {actual}", $"合格 M0：{published}");
    }

    private static ObservatoryCard ForecastCard(JsonElement snapshot, string id, DateTimeOffset created)
    {
        var probability = Percent(Number(snapshot, "qualified_probability"));
        var low = Percent(Number(snapshot, "wilson_90_low"));
        var high = Percent(Number(snapshot, "wilson_90_high"));
        var schedule = "计划启动时间：证据不足";
        if (snapshot.TryGetProperty("schedule_start_recommendation", out var recommendation) && recommendation.ValueKind == JsonValueKind.Object)
        {
            var action = Text(recommendation, "action");
            var at = Text(recommendation, "proposed_start_at");
            schedule = action switch
            {
                "advance" => $"建议提前至 {at}（需审批应用补丁）",
                "delay" => $"建议推迟至 {at}（需审批应用补丁）",
                "hold" => "建议保持当前计划启动时间",
                _ => "计划启动时间：证据不足",
            };
        }
        return new ObservatoryCard(id, created, $"{FriendlyTask(Text(snapshot, "task_key"))} · 交付预测",
            $"10:30 前合格概率 {probability}", $"Wilson 90% 区间 {low}–{high}　证据成熟度 {Maturity(Text(snapshot, "maturity"))}　{schedule}");
    }

    private static ObservatoryCard QualityCard(JsonElement snapshot, JsonElement quality, string id, DateTimeOffset created)
    {
        var coverage = Percent(Number(quality, "evidence_coverage"));
        var groups = Integer(quality, "independent_source_groups");
        var conflicts = Integer(quality, "conflict_count");
        var errors = Integer(quality, "factual_error_count");
        var gate = Boolean(quality, "evidence_gate_passed") is true ? "通过" : "未通过或待确认";
        return new ObservatoryCard(id, created, FriendlyTask(Text(snapshot, "task_key")),
            $"证据覆盖 {coverage}　来源组 {groups}", $"冲突 {conflicts}　事实错误 {errors}　EvidenceGate {gate}");
    }

    private static ObservatoryCard OutcomeCard(JsonElement snapshot, JsonElement outcomes, string id, DateTimeOffset created)
    {
        var parts = outcomes.EnumerateArray().Select(item =>
            $"{Text(item, "horizon")} {OutcomeState(Text(item, "verification_status"), Text(item, "checkpoint_status"))}").ToArray();
        return new ObservatoryCard(id, created, FriendlyTask(Text(snapshot, "task_key")),
            string.Join("　", parts), "按原判断、触发条件和失效条件评测；未完成 checkpoint 不补造结论");
    }

    private static ObservatoryCard ExperimentCard(JsonElement snapshot, string id, DateTimeOffset created)
    {
        var decision = Decision(Text(snapshot, "decision"));
        return new ObservatoryCard(id, created, "进化实验", $"配对运行 {Integer(snapshot, "paired_runs")} · {decision}",
            "交付速度、合格概率、研究质量、判断结果、成本和稳定性分别评测，不计算综合总分");
    }

    private static string Text(JsonElement value, string name) =>
        value.TryGetProperty(name, out var item) && item.ValueKind == JsonValueKind.String ? item.GetString() ?? "" : "";
    private static double? Number(JsonElement value, string name) =>
        value.TryGetProperty(name, out var item) && item.ValueKind == JsonValueKind.Number && item.TryGetDouble(out var number) ? number : null;
    private static int Integer(JsonElement value, string name) =>
        value.TryGetProperty(name, out var item) && item.ValueKind == JsonValueKind.Number && item.TryGetInt32(out var number) ? number : 0;
    private static bool? Boolean(JsonElement value, string name) =>
        value.TryGetProperty(name, out var item) && item.ValueKind is JsonValueKind.True or JsonValueKind.False ? item.GetBoolean() : null;
    private static string Percent(double? value) => value is null ? "暂无" : $"{value:P0}";
    private static string FriendlyTime(string value) => DateTimeOffset.TryParse(value, out var time) ? time.ToString("MM-dd HH:mm", CultureInfo.InvariantCulture) : "暂无";
    private static string FriendlyTask(string value) => value == "daily.execution.0945" ? "09:45 开盘异常" : string.IsNullOrWhiteSpace(value) ? "任务" : value;
    private static string DeliveryState(string value) => value switch
    {
        "qualified" => "按窗口合格", "late" => "迟到", "rejected" => "拒绝", "timeout" => "超时",
        "missed" => "错过", "failed" => "失败", _ => "未完成",
    };
    private static string Maturity(string value) => value switch { "high" => "高", "medium" => "中", "low" => "低", _ => "不足" };
    private static string OutcomeState(string verification, string checkpoint) =>
        checkpoint != "complete" ? "待评测" : verification switch { "correct" => "正确", "incorrect" => "错误", "invalidated" => "已失效", _ => "信息不足" };
    private static string Decision(string value) => value switch
    {
        "recommend_promotion" => "建议晋升", "recommend_rollback" => "建议回滚", "ask_user" => "等待用户选择",
        "reject" => "拒绝候选", _ => "证据不足",
    };
}
