using System.Text.Json;
using System.IO;
using System.Linq;

namespace AITradingCompanion.ToolManager;

public sealed record ToolManagerProjection(string Status, IReadOnlyList<ToolManagerNeed> Needs, IReadOnlyList<ToolManagerTool> Tools, string UpdatedAt)
{
    public static ToolManagerProjection Empty(string status) => new(status, [], [], string.Empty);
}

public sealed record ToolManagerNeed(string NeedId, string Capability, string State, string Urgency, int OccurrenceCount, string UpdatedAt);
public sealed record ToolManagerTool(string Capability, string Version, string Health, string DegradeReason, string AuditReference);

public sealed class ToolManagerProjectionReader
{
    private readonly string _projectionPath;

    public ToolManagerProjectionReader(string? dataDirectory = null)
    {
        var root = dataDirectory ?? Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "AITradingCompanion");
        _projectionPath = Path.Combine(root, "exchange", "tool-manager", "projection.json");
    }

    public ToolManagerProjection Read()
    {
        if (!File.Exists(_projectionPath)) return ToolManagerProjection.Empty("等待 runtime 投影");
        try
        {
            using var document = JsonDocument.Parse(File.ReadAllText(_projectionPath));
            var root = document.RootElement;
            if (root.GetProperty("contract").GetString() != "ai-trading-tool-manager-projection/v1") return ToolManagerProjection.Empty("投影合同无效");
            return new ToolManagerProjection("已连接 Exchange", ReadNeeds(root), ReadTools(root), root.TryGetProperty("updated_at", out var updated) ? updated.GetString() ?? "" : "");
        }
        catch (Exception)
        {
            return ToolManagerProjection.Empty("投影不可读取");
        }
    }

    private static ToolManagerNeed[] ReadNeeds(JsonElement root) => root.TryGetProperty("needs", out var rows) && rows.ValueKind == JsonValueKind.Array
        ? rows.EnumerateArray().Select(row => new ToolManagerNeed(Text(row, "need_id"), Text(row, "capability"), Text(row, "state"), Text(row, "urgency"), row.TryGetProperty("occurrence_count", out var count) ? count.GetInt32() : 0, Text(row, "updated_at"))).ToArray() : [];
    private static ToolManagerTool[] ReadTools(JsonElement root) => root.TryGetProperty("tools", out var rows) && rows.ValueKind == JsonValueKind.Array
        ? rows.EnumerateArray().Select(row => new ToolManagerTool(Text(row, "capability"), Text(row, "version"), Text(row, "health"), Text(row, "degrade_reason"), Text(row, "audit_reference"))).ToArray() : [];
    private static string Text(JsonElement row, string name) => row.TryGetProperty(name, out var value) ? value.GetString() ?? "" : "";
}
