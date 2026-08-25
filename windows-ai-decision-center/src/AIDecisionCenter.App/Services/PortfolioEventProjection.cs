using System.Globalization;
using System.Text.Json;

namespace AIDecisionCenter.App.Services;

public sealed record PortfolioPosition(string Code, string Name, int Shares, double? AverageCost, double? LastPrice, string? PriceAsOf, double? MarketValue, double? UnrealizedPnl, double? Weight, string? UpdatedAt)
{
    public string SharesText => $"{Shares:N0}";
    public string CostText => AverageCost is null ? "暂无" : AverageCost.Value.ToString("N3", CultureInfo.InvariantCulture);
    public string PriceText => LastPrice is null ? "暂无最新价" : LastPrice.Value.ToString("N2", CultureInfo.InvariantCulture);
    public string MarketValueText => MarketValue is null ? "暂无" : MarketValue.Value.ToString("N2", CultureInfo.InvariantCulture);
    public string PnlText => UnrealizedPnl is null ? "暂无" : UnrealizedPnl.Value.ToString("+0.00;-0.00;0.00", CultureInfo.InvariantCulture);
    public string WeightText => Weight is null ? "暂无" : Weight.Value.ToString("P2", CultureInfo.InvariantCulture);
}

public sealed record PortfolioTransaction(string TransactionId, string Action, string Code, string Name, int Shares, double Price, string OccurredAt, string? ReversalOf)
{
    public string Summary => Action == "asset_correction"
        ? $"{OccurredAt} · 总资产修正为 {Price:N2} 元"
        : $"{OccurredAt} · {Action switch { "buy" => "买入", "sell" => "卖出", "position_correction" => "持仓修正", _ => Action }} {Name} {Shares}股 @ {Price:g}{(ReversalOf is null ? string.Empty : " · 撤销事件")}";
}

public sealed record PortfolioWorkspaceProjection(IReadOnlyList<PortfolioPosition> Positions, IReadOnlyList<PortfolioTransaction> Transactions, IReadOnlyDictionary<string, string> StatusByArtifactId, double? TotalAssets, string? UpdatedAt);

public static class PortfolioEventProjection
{
    private const string Contract = "portfolio-client-event/v1";

    public static PortfolioWorkspaceProjection? Project(IEnumerable<string> events)
    {
        var parsed = events.Select(Parse).Where(item => item is not null).Cast<Event>().OrderBy(item => item.At).ToArray();
        if (parsed.Length == 0) return null;
        JsonElement? latestSnapshot = null;
        var statuses = new Dictionary<string, string>(StringComparer.Ordinal);
        foreach (var item in parsed)
        {
            var artifactId = ReadString(item.Payload, "source_artifact_id");
            if (item.Type == "portfolio.snapshot.ready") latestSnapshot = item.Payload;
            else if (item.Payload.TryGetProperty("snapshot", out var nested) && nested.ValueKind == JsonValueKind.Object) latestSnapshot = nested.Clone();
            if (string.IsNullOrWhiteSpace(artifactId)) continue;
            statuses[artifactId] = item.Type switch
            {
                "portfolio.interpretation.started" => "正在识别持仓信息",
                "portfolio.change.applied" => $"持仓已更新：{ReadString(item.Payload, "summary")}",
                "portfolio.change.needs_input" => $"需要补充：{ReadStringArray(item.Payload, "missing_fields")}",
                "portfolio.change.rejected" => ReadString(item.Payload, "reason") ?? "未作为真实成交处理",
                _ => statuses.GetValueOrDefault(artifactId, string.Empty),
            };
        }
        if (latestSnapshot is null) return new PortfolioWorkspaceProjection([], [], statuses, null, null);
        var snapshot = latestSnapshot.Value;
        return new PortfolioWorkspaceProjection(ReadArray(snapshot, "positions", ParsePosition), ReadArray(snapshot, "transactions", ParseTransaction), statuses, ReadDouble(snapshot, "total_assets"), ReadString(snapshot, "updated_at"));
    }

    private static PortfolioPosition ParsePosition(JsonElement item)
    {
        return new PortfolioPosition(ReadString(item, "code") ?? "", ReadString(item, "name") ?? "", ReadInt(item, "shares"), ReadDouble(item, "average_cost"), ReadDouble(item, "last_price"), ReadString(item, "price_as_of"), ReadDouble(item, "market_value"), ReadDouble(item, "unrealized_pnl"), ReadDouble(item, "weight"), ReadString(item, "updated_at"));
    }

    private static PortfolioTransaction ParseTransaction(JsonElement item) => new(ReadString(item, "transaction_id") ?? "", ReadString(item, "action") ?? "", ReadString(item, "code") ?? "", ReadString(item, "name") ?? "", ReadInt(item, "shares"), ReadDouble(item, "price") ?? 0, ReadString(item, "occurred_at") ?? "", ReadString(item, "reversal_of"));

    private static Event? Parse(string json)
    {
        try
        {
            using var document = JsonDocument.Parse(json);
            var root = document.RootElement;
            if (ReadString(root, "contract") != Contract || !root.TryGetProperty("payload", out var payload) || payload.ValueKind != JsonValueKind.Object) return null;
            return new Event(ReadString(root, "type") ?? "", DateTimeOffset.TryParse(ReadString(root, "created_at"), out var at) ? at : DateTimeOffset.MinValue, payload.Clone());
        }
        catch (JsonException) { return null; }
    }

    private static T[] ReadArray<T>(JsonElement root, string name, Func<JsonElement, T> map) => root.TryGetProperty(name, out var array) && array.ValueKind == JsonValueKind.Array ? array.EnumerateArray().Select(map).ToArray() : [];
    private static string? ReadString(JsonElement root, string name) => root.TryGetProperty(name, out var value) && value.ValueKind == JsonValueKind.String ? value.GetString() : null;
    private static double? ReadDouble(JsonElement root, string name) => root.TryGetProperty(name, out var value) && value.ValueKind == JsonValueKind.Number ? value.GetDouble() : null;
    private static int ReadInt(JsonElement root, string name) => root.TryGetProperty(name, out var value) && value.ValueKind == JsonValueKind.Number ? value.GetInt32() : 0;
    private static string ReadStringArray(JsonElement root, string name) => root.TryGetProperty(name, out var value) && value.ValueKind == JsonValueKind.Array ? string.Join("、", value.EnumerateArray().Select(item => item.GetString())) : "必要信息";
    private sealed record Event(string Type, DateTimeOffset At, JsonElement Payload);
}
