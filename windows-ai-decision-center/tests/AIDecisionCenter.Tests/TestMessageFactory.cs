using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Globalization;
using AIDecisionCenter.Core.Models;

namespace AIDecisionCenter.Tests;

internal static class TestMessageFactory
{
    public static string CreateEnvelope(
        string? runId = null,
        string body = "# 操作决策\n\n保持仓位。",
        string status = "succeeded",
        string taskKey = "daily.execution.1430")
    {
        runId ??= Guid.NewGuid().ToString();
        var hash = Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(body))).ToLowerInvariant();
        return JsonSerializer.Serialize(new Dictionary<string, object?>
        {
            ["contract"] = "ai-decision-message/v1",
            ["message_id"] = $"stock-advisor:{runId}",
            ["source"] = "stock_advisor",
            ["run_id"] = runId,
            ["project"] = "A股",
            ["task_key"] = taskKey,
            ["task_type"] = "操作决策",
            ["scheduled_for"] = "2026-08-24T14:30:00+08:00",
            ["completed_at"] = "2026-08-24T14:36:12+08:00",
            ["status"] = status,
            ["registry_id"] = "ScheduleRegistry-local-v1.4",
            ["protocol_id"] = "DailyExecution-v1.5",
            ["summary"] = "保持仓位，等待进一步确认。",
            ["body_markdown"] = body,
            ["payload"] = new Dictionary<string, object?> { ["actions"] = Array.Empty<object>() },
            ["response_sha256"] = hash
        });
    }

    public static IncomingTaskMessage CreateIncoming(string? runId = null)
    {
        runId ??= Guid.NewGuid().ToString();
        const string body = "# 操作决策\n\n保持仓位。";
        var hash = Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(body))).ToLowerInvariant();
        return new IncomingTaskMessage(
            runId,
            "stock_advisor",
            runId,
            "A股",
            "daily.execution.1430",
            "操作决策",
            DateTimeOffset.Parse("2026-08-24T14:30:00+08:00", CultureInfo.InvariantCulture),
            DateTimeOffset.Parse("2026-08-24T14:36:12+08:00", CultureInfo.InvariantCulture),
            DateTimeOffset.UtcNow,
            TaskMessageStatus.Succeeded,
            "ScheduleRegistry-local-v1.4",
            "DailyExecution-v1.5",
            "保持仓位，等待进一步确认。",
            body,
            "{\"actions\":[]}",
            hash);
    }
}
