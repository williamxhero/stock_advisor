using System.Globalization;
using System.Text.Json;

namespace AITradingCompanion.Desktop.Services;

public sealed record CompanionAiTimelineEntry(
    string ArtifactId,
    string Kind,
    DateTimeOffset At,
    string Text,
    DateTimeOffset? StartedAt = null,
    DateTimeOffset? CompletedAt = null);

public sealed record CompanionTimelineEntry(
    DateTimeOffset At,
    string Text,
    bool CountsForM1,
    string? ArtifactId = null,
    string? MessageId = null,
    string State = "submitted",
    string Phase = "h0");

public sealed record CompanionWorkspaceProjection(
    string CycleId,
    DateTimeOffset? ScheduledFor,
    DateTimeOffset? H0AutoSubmitAt,
    DateTimeOffset? M1PublishDeadline,
    DateTimeOffset? H0LockedAt,
    string? State,
    string? ErrorText,
    IReadOnlyList<CompanionAiTimelineEntry> AiMessages,
    IReadOnlyList<CompanionTimelineEntry> UserMessages)
{
    public bool IsH0Locked => H0LockedAt is not null;
    public bool HasStagedMessages => UserMessages.Any(message => message.State == "staged");
}

public static class CompanionEventProjection
{
    private const string Contract = "companion-client-event/v1";

    public static bool TryGetLatestCycleId(IEnumerable<string> events, out string cycleId)
    {
        var projection = Project(events);
        cycleId = projection?.CycleId ?? string.Empty;
        return projection is not null;
    }

    public static CompanionWorkspaceProjection? Project(IEnumerable<string> events) => ProjectForTask(events, null);

    public static CompanionWorkspaceProjection? ProjectForTask(IEnumerable<string> events, string? taskKey)
    {
        var parsed = events.Select(TryParse).Where(item => item is not null).Cast<CompanionEvent>()
            .OrderBy(item => item.At).ToArray();
        if (!string.IsNullOrWhiteSpace(taskKey))
            parsed = parsed.Where(item => string.Equals(item.TaskKey, taskKey, StringComparison.Ordinal)).ToArray();
        if (parsed.Length == 0) return null;

        var current = parsed[^1];
        var cycleEvents = parsed.Where(item => item.CycleId == current.CycleId).ToArray();
        DateTimeOffset? scheduledFor = null;
        DateTimeOffset? autoSubmit = null;
        DateTimeOffset? m1Deadline = null;
        DateTimeOffset? h0LockedAt = null;
        DateTimeOffset? m0StartedAt = null;
        DateTimeOffset? m1StartedAt = null;
        DateTimeOffset? m2StartedAt = null;
        string? state = null;
        string? errorText = null;
        var ai = new Dictionary<string, CompanionAiTimelineEntry>(StringComparer.Ordinal);
        var users = new Dictionary<string, CompanionTimelineEntry>(StringComparer.Ordinal);

        foreach (var item in cycleEvents)
        {
            var payload = item.Payload;
            state = ReadCycleString(payload, "state") ?? state;
            scheduledFor = ReadDate(ReadCycleString(payload, "scheduled_for")) ?? scheduledFor;
            autoSubmit = ReadDate(ReadCycleString(payload, "h0_auto_submit_at"))
                ?? ReadDate(ReadString(payload, "h0_auto_submit_at")) ?? autoSubmit;
            m1Deadline = ReadDate(ReadCycleString(payload, "m1_publish_deadline"))
                ?? ReadDate(ReadString(payload, "m1_publish_deadline")) ?? m1Deadline;
            h0LockedAt = ReadDate(ReadCycleString(payload, "h0_locked_at")) ?? h0LockedAt;

            switch (item.Type)
            {
                case "m0.started":
                case "research.started":
                    m0StartedAt ??= item.At;
                    break;
                case "m0.ready":
                    UpsertAi(ai, ReadString(payload, "source_artifact_id"), "m0", item.At,
                        ReadString(payload, "m0"), m0StartedAt, item.At);
                    break;
                case "brief.ready": // v1 compatibility
                    UpsertAi(ai, null, "m0", item.At, ReadString(payload, "brief"), m0StartedAt, item.At);
                    autoSubmit = ReadDate(ReadString(payload, "human_deadline")) ?? autoSubmit;
                    break;
                case "h0.locked":
                    h0LockedAt ??= item.At;
                    ReadUserMessages(payload, users, "submitted", "h0", item.At);
                    m1StartedAt ??= item.At;
                    break;
                case "message.staged":
                    if (payload.TryGetProperty("message", out var staged))
                        UpsertUser(users, staged, "staged", item.At);
                    break;
                case "message.edited":
                    if (payload.TryGetProperty("message", out var edited))
                        UpsertUser(users, edited, "staged", item.At);
                    break;
                case "message.withdrawn":
                    if (ReadString(payload, "message_id") is { } withdrawn) users.Remove(withdrawn);
                    break;
                case "pre_m0.locked":
                case "pre_m0.submitted":
                    ReadUserMessages(payload, users, "submitted", "pre_m0", item.At);
                    break;
                case "human.message_batch.accepted":
                    ReadUserMessages(payload, users, "submitted", "chat", item.At);
                    break;
                case "m1.started":
                    m1StartedAt ??= item.At;
                    break;
                case "m1.ready":
                    foreach (var fault in ai.Where(pair => pair.Value.Kind == "fault").Select(pair => pair.Key).ToArray())
                        ai.Remove(fault);
                    errorText = null;
                    UpsertAi(ai, ReadString(payload, "source_artifact_id"), "m1", item.At,
                        ReadString(payload, "m1"), m1StartedAt, item.At);
                    break;
                case "m1.recovered":
                    foreach (var fault in ai.Where(pair => pair.Value.Kind == "fault").Select(pair => pair.Key).ToArray())
                        ai.Remove(fault);
                    errorText = null;
                    UpsertAi(ai, $"recovery-{item.At:O}", "recovery", item.At,
                        ReadString(payload, "message"), item.At, item.At);
                    break;
                case "joint.ready": // v1 compatibility; this mixed H0 and the old model output.
                    UpsertAi(ai, null, "legacy_synthesis", item.At,
                        ReadString(payload, "m1"), m1StartedAt, item.At);
                    break;
                case "m0.revealed": // v1 compatibility
                    UpsertAi(ai, null, "legacy_model", item.At, ReadString(payload, "m0"), m1StartedAt, item.At);
                    break;
                case "m2.started":
                    m2StartedAt ??= item.At;
                    break;
                case "m2.ready":
                    foreach (var fault in ai.Where(pair => pair.Value.Kind == "fault").Select(pair => pair.Key).ToArray())
                        ai.Remove(fault);
                    errorText = null;
                    UpsertAi(ai, ReadString(payload, "source_artifact_id"), "m2", item.At,
                        ReadString(payload, "m2"), m2StartedAt, item.At);
                    break;
                case "chat.ready":
                    UpsertAi(ai, ReadString(payload, "stream_id") ?? ReadString(payload, "source_artifact_id"), "chat", item.At,
                        ReadString(payload, "text"), item.At, item.At);
                    break;
                case "chat.stream.delta":
                    var streamId = ReadString(payload, "stream_id");
                    if (!string.IsNullOrWhiteSpace(streamId))
                    {
                        var existingText = ai.TryGetValue(streamId, out var existing) ? existing.Text : string.Empty;
                        UpsertAi(ai, streamId, "chat", item.At, existingText + (ReadString(payload, "text") ?? string.Empty), item.At, null);
                    }
                    break;
                case "chat.stream.failed":
                    var failedStreamId = ReadNestedString(payload, "stream", "stream_id");
                    var failedText = ReadNestedString(payload, "stream", "text");
                    if (!string.IsNullOrWhiteSpace(failedStreamId) && !string.IsNullOrWhiteSpace(failedText))
                        UpsertAi(ai, failedStreamId, "chat_incomplete", item.At, failedText + "\n\n（未完成）", item.At, null);
                    UpsertAi(ai, ReadString(payload, "source_artifact_id"), "fault", item.At, ReadString(payload, "reason"), item.At, item.At);
                    break;
                case "premarket.reply.ready":
                    UpsertAi(ai, ReadString(payload, "source_artifact_id"), "premarket", item.At,
                        ReadString(payload, "text"), item.At, item.At);
                    break;
                case "outcome.ready":
                    UpsertAi(ai, ReadString(payload, "source_artifact_id"), "outcome", item.At,
                        ReadString(payload, "text"), item.At, item.At);
                    break;
                case "reflection.ready":
                    UpsertAi(ai, ReadString(payload, "source_artifact_id"), "reflection", item.At,
                        ReadString(payload, "text"), item.At, item.At);
                    break;
                case "judgment.revised":
                    UpsertAi(ai, ReadString(payload, "source_artifact_id"), "judgment_revision", item.At,
                        ReadString(payload, "text"), item.At, item.At);
                    break;
                case "projection.ready":
                    ReadProjection(payload, ai, users, ref scheduledFor, ref autoSubmit, ref m1Deadline, ref h0LockedAt);
                    break;
                case "research.failed":
                case "cycle.missed":
                case "m1.failed":
                case "outcome.failed":
                case "chat_research.failed":
                    errorText = ReadString(payload, "reason") ?? "本次 AI 研究未完成。";
                    UpsertAi(ai, $"fault-{item.At:O}", "fault", item.At, errorText, item.At, item.At);
                    break;
                case "m2.deferred":
                    errorText = ReadString(payload, "reason") ?? "M2 已延后。";
                    UpsertAi(ai, $"fault-{item.At:O}", "fault", item.At, errorText, item.At, item.At);
                    break;
            }
        }

        return new CompanionWorkspaceProjection(
            current.CycleId,
            scheduledFor,
            autoSubmit,
            m1Deadline,
            h0LockedAt,
            state,
            errorText,
            ai.Values.OrderBy(message => message.At).ToArray(),
            users.Values.OrderBy(message => message.At).ToArray());
    }

    private static void ReadProjection(
        JsonElement payload,
        Dictionary<string, CompanionAiTimelineEntry> ai,
        Dictionary<string, CompanionTimelineEntry> users,
        ref DateTimeOffset? scheduledFor,
        ref DateTimeOffset? autoSubmit,
        ref DateTimeOffset? m1Deadline,
        ref DateTimeOffset? h0LockedAt)
    {
        scheduledFor = ReadDate(ReadNestedString(payload, "cycle", "scheduled_for")) ?? scheduledFor;
        autoSubmit = ReadDate(ReadNestedString(payload, "cycle", "h0_auto_submit_at")) ?? autoSubmit;
        m1Deadline = ReadDate(ReadNestedString(payload, "cycle", "m1_publish_deadline")) ?? m1Deadline;
        h0LockedAt = ReadDate(ReadNestedString(payload, "cycle", "h0_locked_at")) ?? h0LockedAt;
        var projectedM1StartedAt = ReadDate(ReadNestedString(payload, "cycle", "m1_started_at"));
        var projectedM1CompletedAt = ReadDate(ReadNestedString(payload, "cycle", "m1_completed_at"));
        var projectedM2StartedAt = ReadDate(ReadNestedString(payload, "cycle", "m2_started_at"));
        var projectedM2CompletedAt = ReadDate(ReadNestedString(payload, "cycle", "m2_completed_at"));
        if (payload.TryGetProperty("ai_messages", out var aiMessages) && aiMessages.ValueKind == JsonValueKind.Array)
        {
            foreach (var message in aiMessages.EnumerateArray())
            {
                var kind = ReadString(message, "kind") switch
                {
                    "premarket_chat" => "premarket",
                    { } value => value,
                    _ => "chat",
                };
                var at = ReadDate(ReadString(message, "at")) ?? DateTimeOffset.MinValue;
                var started = kind == "m1" ? projectedM1StartedAt : kind == "m2" ? projectedM2StartedAt : at;
                var completed = kind == "m1" ? projectedM1CompletedAt : kind == "m2" ? projectedM2CompletedAt : at;
                UpsertAi(ai, ReadString(message, "artifact_id"), kind, at, ReadString(message, "text"), started ?? at, completed ?? at);
            }
        }
        if (payload.TryGetProperty("user_messages", out var userMessages) && userMessages.ValueKind == JsonValueKind.Array)
        {
            foreach (var message in userMessages.EnumerateArray()) UpsertUser(users, message, null, DateTimeOffset.MinValue);
        }
        if (payload.TryGetProperty("stream_messages", out var streamMessages) && streamMessages.ValueKind == JsonValueKind.Array)
        {
            foreach (var stream in streamMessages.EnumerateArray())
            {
                var id = ReadString(stream, "stream_id");
                var text = ReadString(stream, "text");
                if (string.IsNullOrWhiteSpace(id) || string.IsNullOrWhiteSpace(text)) continue;
                var state = ReadString(stream, "state");
                if (state == "failed") text += "\n\n（未完成）";
                var at = ReadDate(ReadString(stream, "created_at")) ?? DateTimeOffset.MinValue;
                UpsertAi(ai, id, state == "failed" ? "chat_incomplete" : "chat", at, text, at, state == "completed" ? ReadDate(ReadString(stream, "completed_at")) : null);
            }
        }
    }

    private static void ReadUserMessages(
        JsonElement payload,
        Dictionary<string, CompanionTimelineEntry> users,
        string state,
        string phase,
        DateTimeOffset fallbackAt)
    {
        if (!payload.TryGetProperty("messages", out var messages) || messages.ValueKind != JsonValueKind.Array) return;
        foreach (var message in messages.EnumerateArray()) UpsertUser(users, message, state, fallbackAt, phase);
    }

    private static void UpsertUser(
        Dictionary<string, CompanionTimelineEntry> users,
        JsonElement message,
        string? stateOverride,
        DateTimeOffset fallbackAt,
        string? phaseOverride = null)
    {
        var id = ReadString(message, "message_id") ?? Guid.NewGuid().ToString();
        var text = ReadString(message, "body_text") ?? ReadString(message, "text");
        if (string.IsNullOrWhiteSpace(text)) return;
        var state = stateOverride ?? ReadString(message, "state") ?? "submitted";
        var phase = phaseOverride ?? ReadString(message, "phase") ?? "h0";
        var at = ReadDate(ReadString(message, "submitted_at"))
            ?? ReadDate(ReadString(message, "staged_at"))
            ?? ReadDate(ReadString(message, "at"))
            ?? fallbackAt;
        users[id] = new CompanionTimelineEntry(
            at, text, state == "submitted" && phase == "h0",
            ReadString(message, "source_artifact_id"), id, state, phase);
    }

    private static void UpsertAi(
        Dictionary<string, CompanionAiTimelineEntry> ai,
        string? artifactId,
        string kind,
        DateTimeOffset at,
        string? text,
        DateTimeOffset? started,
        DateTimeOffset? completed)
    {
        if (string.IsNullOrWhiteSpace(text)) return;
        var id = artifactId ?? $"{kind}-{at:O}";
        if (ai.TryGetValue(id, out var existing))
        {
            started = existing.StartedAt ?? started;
            completed = existing.CompletedAt ?? completed;
        }
        ai[id] = new CompanionAiTimelineEntry(id, kind, at, text, started, completed);
    }

    private static CompanionEvent? TryParse(string json)
    {
        try
        {
            using var document = JsonDocument.Parse(json);
            var root = document.RootElement;
            if (ReadString(root, "contract") != Contract
                || string.IsNullOrWhiteSpace(ReadString(root, "cycle_id"))
                || string.IsNullOrWhiteSpace(ReadString(root, "type"))
                || !root.TryGetProperty("payload", out var payload)
                || payload.ValueKind != JsonValueKind.Object) return null;
            return new CompanionEvent(
                ReadString(root, "cycle_id")!,
                ReadString(root, "type")!,
                ReadDate(ReadString(root, "created_at")) ?? DateTimeOffset.MinValue,
                ReadCycleString(payload, "task_key"),
                payload.Clone());
        }
        catch (JsonException) { return null; }
    }

    private static string? ReadString(JsonElement element, string property) =>
        element.TryGetProperty(property, out var value) && value.ValueKind == JsonValueKind.String ? value.GetString() : null;

    private static string? ReadNestedString(JsonElement element, string parent, string property) =>
        element.TryGetProperty(parent, out var nested) && nested.ValueKind == JsonValueKind.Object ? ReadString(nested, property) : null;

    private static string? ReadCycleString(JsonElement payload, string property) =>
        ReadNestedString(payload, "cycle", property) ?? ReadString(payload, property);

    private static DateTimeOffset? ReadDate(string? value) =>
        DateTimeOffset.TryParse(value, CultureInfo.InvariantCulture, DateTimeStyles.RoundtripKind, out var result) ? result : null;

    private sealed record CompanionEvent(string CycleId, string Type, DateTimeOffset At, string? TaskKey, JsonElement Payload);
}
