using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.Json.Serialization;
using AITradingCompanion.Core.Models;

namespace AITradingCompanion.Core.Parsing;

public static class DecisionMessageParser
{
    public const string Contract = "ai-decision-message/v1";

    private static readonly JsonSerializerOptions JsonOptions = new()
    {
        PropertyNameCaseInsensitive = false,
        UnmappedMemberHandling = JsonUnmappedMemberHandling.Disallow
    };

    public static bool TryParse(
        string json,
        DateTimeOffset receivedAt,
        out IncomingTaskMessage? message,
        out string? error)
    {
        message = null;
        error = null;
        Envelope? envelope;
        try
        {
            envelope = JsonSerializer.Deserialize<Envelope>(json, JsonOptions);
        }
        catch (JsonException exception)
        {
            error = $"JSON无效：{exception.Message}";
            return false;
        }

        if (envelope is null)
        {
            error = "消息为空";
            return false;
        }

        if (!string.Equals(envelope.Contract, Contract, StringComparison.Ordinal))
        {
            error = $"不支持的contract：{envelope.Contract}";
            return false;
        }

        if (!string.Equals(envelope.Source, "stock_advisor", StringComparison.Ordinal))
        {
            error = $"不支持的source：{envelope.Source}";
            return false;
        }

        if (!Guid.TryParse(envelope.RunId, out _) ||
            !string.Equals(envelope.MessageId, $"stock-advisor:{envelope.RunId}", StringComparison.Ordinal))
        {
            error = "message_id与run_id不匹配";
            return false;
        }

        foreach (var (name, value) in RequiredText(envelope))
        {
            if (string.IsNullOrWhiteSpace(value))
            {
                error = $"缺少必填字段：{name}";
                return false;
            }
        }

        if (envelope.Summary!.Length > 240)
        {
            error = "summary超过240字符";
            return false;
        }

        if (!DateTimeOffset.TryParse(envelope.ScheduledFor, out var scheduledFor) ||
            !DateTimeOffset.TryParse(envelope.CompletedAt, out var completedAt))
        {
            error = "scheduled_for或completed_at不是有效的带偏移时间";
            return false;
        }

        var status = envelope.Status switch
        {
            "succeeded" => TaskMessageStatus.Succeeded,
            "skipped" => TaskMessageStatus.Skipped,
            "failed" => TaskMessageStatus.Failed,
            _ => (TaskMessageStatus?)null
        };
        if (status is null)
        {
            error = $"不支持的status：{envelope.Status}";
            return false;
        }

        if (envelope.Payload.ValueKind != JsonValueKind.Object)
        {
            error = "payload必须是JSON object";
            return false;
        }

        var hash = Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(envelope.BodyMarkdown!))).ToLowerInvariant();
        if (!string.Equals(hash, envelope.ResponseSha256, StringComparison.Ordinal))
        {
            error = "response_sha256校验失败";
            return false;
        }

        message = new IncomingTaskMessage(
            envelope.RunId!,
            envelope.Source!,
            envelope.RunId,
            envelope.Project!,
            envelope.TaskKey!,
            envelope.TaskType!,
            scheduledFor,
            completedAt,
            receivedAt,
            status.Value,
            envelope.RegistryId!,
            envelope.ProtocolId!,
            envelope.Summary!,
            envelope.BodyMarkdown!,
            envelope.Payload.GetRawText(),
            hash);
        return true;
    }

    private static IEnumerable<(string Name, string? Value)> RequiredText(Envelope envelope)
    {
        yield return ("message_id", envelope.MessageId);
        yield return ("run_id", envelope.RunId);
        yield return ("project", envelope.Project);
        yield return ("task_key", envelope.TaskKey);
        yield return ("task_type", envelope.TaskType);
        yield return ("scheduled_for", envelope.ScheduledFor);
        yield return ("completed_at", envelope.CompletedAt);
        yield return ("status", envelope.Status);
        yield return ("registry_id", envelope.RegistryId);
        yield return ("protocol_id", envelope.ProtocolId);
        yield return ("summary", envelope.Summary);
        yield return ("body_markdown", envelope.BodyMarkdown);
        yield return ("response_sha256", envelope.ResponseSha256);
    }

    private sealed class Envelope
    {
        [JsonPropertyName("contract")]
        public string? Contract { get; init; }

        [JsonPropertyName("message_id")]
        public string? MessageId { get; init; }

        [JsonPropertyName("source")]
        public string? Source { get; init; }

        [JsonPropertyName("run_id")]
        public string? RunId { get; init; }

        [JsonPropertyName("project")]
        public string? Project { get; init; }

        [JsonPropertyName("task_key")]
        public string? TaskKey { get; init; }

        [JsonPropertyName("task_type")]
        public string? TaskType { get; init; }

        [JsonPropertyName("scheduled_for")]
        public string? ScheduledFor { get; init; }

        [JsonPropertyName("completed_at")]
        public string? CompletedAt { get; init; }

        [JsonPropertyName("status")]
        public string? Status { get; init; }

        [JsonPropertyName("registry_id")]
        public string? RegistryId { get; init; }

        [JsonPropertyName("protocol_id")]
        public string? ProtocolId { get; init; }

        [JsonPropertyName("summary")]
        public string? Summary { get; init; }

        [JsonPropertyName("body_markdown")]
        public string? BodyMarkdown { get; init; }

        [JsonPropertyName("payload")]
        public JsonElement Payload { get; init; }

        [JsonPropertyName("response_sha256")]
        public string? ResponseSha256 { get; init; }
    }
}
