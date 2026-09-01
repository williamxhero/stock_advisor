using System.Globalization;
using System.Text.Json;

namespace AITradingCompanion.Desktop.Services;

public sealed record CompanionMessagePart(
    string Kind,
    string Text,
    string? SourceTitle = null,
    string? SourceUrl = null);

public sealed record CompanionAiTimelineEntry(
    string ArtifactId,
    string Kind,
    DateTimeOffset At,
    string Text,
    DateTimeOffset? StartedAt = null,
    DateTimeOffset? CompletedAt = null,
    IReadOnlyList<CompanionMessagePart>? Parts = null);

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
    IReadOnlyList<CompanionTimelineEntry> UserMessages,
    string? TaskKey = null,
    string? Trigger = null,
    DateTimeOffset? RequestedAt = null,
    string? TaskProfileId = null,
    string? TaskProfileDisplayName = null,
    bool IsDismissed = false,
    bool IsCompanionThinking = false)
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
        return ProjectEvents(parsed);
    }

    public static CompanionWorkspaceProjection? ProjectForCycle(IEnumerable<string> events, string cycleId) =>
        ProjectEvents(events.Select(TryParse).Where(item => item is not null).Cast<CompanionEvent>()
            .Where(item => string.Equals(item.CycleId, cycleId, StringComparison.Ordinal))
            .OrderBy(item => item.At).ToArray());

    public static IReadOnlyList<CompanionWorkspaceProjection> ProjectAll(IEnumerable<string> events) =>
        events.Select(TryParse).Where(item => item is not null).Cast<CompanionEvent>()
            .GroupBy(item => item.CycleId, StringComparer.Ordinal)
            .Select(group => ProjectEvents(group.OrderBy(item => item.At).ToArray()))
            .Where(item => item is not null).Cast<CompanionWorkspaceProjection>()
            .OrderBy(item => item.RequestedAt ?? item.ScheduledFor ?? DateTimeOffset.MinValue)
            .ToArray();

    private static CompanionWorkspaceProjection? ProjectEvents(CompanionEvent[] parsed)
    {
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
        DateTimeOffset? requestedAt = null;
        string? state = null;
        string? errorText = null;
        string? taskKey = null;
        string? trigger = null;
        string? taskProfileId = null;
        string? taskProfileDisplayName = null;
        var isDismissed = false;
        var isCompanionThinking = false;
        var ai = new Dictionary<string, CompanionAiTimelineEntry>(StringComparer.Ordinal);
        var users = new Dictionary<string, CompanionTimelineEntry>(StringComparer.Ordinal);

        foreach (var item in cycleEvents)
        {
            var payload = item.Payload;
            state = ReadCycleString(payload, "state") ?? state;
            taskKey = ReadCycleString(payload, "task_key") ?? taskKey;
            trigger = ReadCycleString(payload, "trigger") ?? trigger;
            requestedAt = ReadDate(ReadCycleString(payload, "requested_at")) ?? requestedAt;
            taskProfileId = ReadCycleString(payload, "task_profile_id") ?? taskProfileId;
            taskProfileDisplayName = ReadTaskProfileDisplayName(payload) ?? taskProfileDisplayName;
            scheduledFor = ReadDate(ReadCycleString(payload, "scheduled_for")) ?? scheduledFor;
            autoSubmit = ReadDate(ReadCycleString(payload, "h0_auto_submit_at"))
                ?? ReadDate(ReadString(payload, "h0_auto_submit_at")) ?? autoSubmit;
            m1Deadline = ReadDate(ReadCycleString(payload, "m1_publish_deadline"))
                ?? ReadDate(ReadString(payload, "m1_publish_deadline")) ?? m1Deadline;
            h0LockedAt = ReadDate(ReadCycleString(payload, "h0_locked_at")) ?? h0LockedAt;

            switch (item.Type)
            {
                case "analysis.dismissed":
                    isDismissed = true;
                    break;
                case "m0.started":
                case "research.started":
                    m0StartedAt ??= item.At;
                    UpsertAi(ai, "action-pending-m0", "action_pending", item.At,
                        "AI 正在研究中", m0StartedAt, null);
                    break;
                case "research.retrying":
                    var pendingResearchId = ai.ContainsKey("action-pending-m0")
                        ? "action-pending-m0"
                        : ai.ContainsKey("action-pending-m1") ? "action-pending-m1" : null;
                    if (pendingResearchId is not null)
                        UpsertAi(ai, pendingResearchId, "action_pending", item.At,
                            ReadString(payload, "reason") ?? "AI 正在重新尝试研究", item.At, null);
                    break;
                case "m0.ready":
                    ai.Remove("action-pending-m0");
                    UpsertAi(ai, ReadString(payload, "source_artifact_id"), "m0", item.At,
                        ReadPublishedText(payload, "m0"), m0StartedAt, item.At, ReadPublishedParts(payload));
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
                    UpsertAi(ai, "action-pending-m1", "action_pending", item.At,
                        "AI 正在形成独立判断", m1StartedAt, null);
                    break;
                case "m1.ready":
                    ai.Remove("action-pending-m1");
                    foreach (var fault in ai.Where(pair => pair.Value.Kind == "fault").Select(pair => pair.Key).ToArray())
                        ai.Remove(fault);
                    errorText = null;
                    UpsertAi(ai, ReadString(payload, "source_artifact_id"), "m1", item.At,
                        ReadPublishedText(payload, "m1"), m1StartedAt, item.At, ReadPublishedParts(payload));
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
                    UpsertAi(ai, "action-pending-m2", "action_pending", item.At,
                        "AI 正在综合判断", m2StartedAt, null);
                    break;
                case "m2.ready":
                    ai.Remove("action-pending-m2");
                    foreach (var fault in ai.Where(pair => pair.Value.Kind == "fault").Select(pair => pair.Key).ToArray())
                        ai.Remove(fault);
                    errorText = null;
                    UpsertAi(ai, ReadString(payload, "source_artifact_id"), "m2", item.At,
                        ReadPublishedText(payload, "m2"), m2StartedAt, item.At, ReadPublishedParts(payload));
                    break;
                case "chat.ready":
                    isCompanionThinking = false;
                    UpsertAi(ai, ReadString(payload, "stream_id") ?? ReadString(payload, "source_artifact_id"), "chat", item.At,
                        ReadPublishedText(payload), item.At, item.At, ReadPublishedParts(payload));
                    break;
                case "chat.stream.started":
                    isCompanionThinking = true;
                    break;
                case "chat.stream.delta":
                    isCompanionThinking = false;
                    var streamId = ReadString(payload, "stream_id");
                    if (!string.IsNullOrWhiteSpace(streamId))
                    {
                        var existingText = ai.TryGetValue(streamId, out var existing) && existing.Kind != "chat_pending"
                            ? existing.Text
                            : string.Empty;
                        UpsertAi(ai, streamId, "chat", item.At, existingText + (ReadPublishedText(payload) ?? string.Empty), item.At, null);
                    }
                    break;
                case "chat.stream.failed":
                    isCompanionThinking = false;
                    var failedStreamId = ReadNestedString(payload, "stream", "stream_id");
                    var failedText = ReadNestedString(payload, "stream", "text");
                    if (!string.IsNullOrWhiteSpace(failedStreamId) && !string.IsNullOrWhiteSpace(failedText))
                        UpsertAi(ai, failedStreamId, "chat_incomplete", item.At, failedText + "\n\n（未完成）", item.At, null);
                    UpsertAi(ai, ReadString(payload, "source_artifact_id"), "fault", item.At, ReadString(payload, "reason"), item.At, item.At);
                    break;
                case "chat.research.terminated":
                    foreach (var pending in ai.Where(pair => pair.Value.Kind == "chat_pending").Select(pair => pair.Key).ToArray())
                        ai.Remove(pending);
                    UpsertAi(ai, $"chat-control-{item.At:O}", "chat_terminated", item.At, "研究已终止，尚未形成完整结论。", item.At, item.At);
                    break;
                case "chat.stream.cancelled":
                    var cancelledStreamId = ReadString(payload, "stream_id");
                    if (!string.IsNullOrWhiteSpace(cancelledStreamId)) ai.Remove(cancelledStreamId);
                    break;
                case "chat.research.continued":
                    UpsertAi(ai, $"chat-control-{item.At:O}", "chat_pending", item.At, "正在继续核验。", item.At, null);
                    break;
                case "premarket.reply.ready":
                    UpsertAi(ai, ReadString(payload, "source_artifact_id"), "premarket", item.At,
                        ReadPublishedText(payload), item.At, item.At, ReadPublishedParts(payload));
                    break;
                case "outcome.ready":
                    UpsertAi(ai, ReadString(payload, "source_artifact_id"), "outcome", item.At,
                        ReadPublishedText(payload), item.At, item.At, ReadPublishedParts(payload));
                    break;
                case "reflection.ready":
                    UpsertAi(ai, ReadString(payload, "source_artifact_id"), "reflection", item.At,
                        ReadPublishedText(payload), item.At, item.At, ReadPublishedParts(payload));
                    break;
                case "judgment.revised":
                    UpsertAi(ai, ReadString(payload, "source_artifact_id"), "judgment_revision", item.At,
                        ReadPublishedText(payload), item.At, item.At, ReadPublishedParts(payload));
                    break;
                case "projection.ready":
                    ReadProjection(payload, ai, users, ref scheduledFor, ref autoSubmit, ref m1Deadline, ref h0LockedAt,
                        ref taskKey, ref trigger, ref requestedAt, ref taskProfileId, ref taskProfileDisplayName,
                        ref isCompanionThinking);
                    break;
                case "research.failed":
                case "m0.invalidated":
                case "cycle.missed":
                case "m1.failed":
                case "outcome.failed":
                case "chat_research.failed":
                    RemoveActionPending(ai);
                    errorText = ReadNestedString(payload, "message", "text_projection")
                        ?? ReadString(payload, "reason") ?? "这次研究没有完成。";
                    UpsertAi(ai, ReadString(payload, "source_artifact_id") ?? $"fault-{item.At:O}", "fault", item.At,
                        errorText, item.At, item.At, ReadPublishedParts(payload));
                    break;
                case "m2.deferred":
                    errorText = ReadNestedString(payload, "message", "text_projection")
                        ?? ReadString(payload, "reason") ?? "这次综合判断要晚一点。";
                    UpsertAi(ai, ReadString(payload, "source_artifact_id") ?? $"fault-{item.At:O}", "fault", item.At,
                        errorText, item.At, item.At, ReadPublishedParts(payload));
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
            CollapseVisibleFaults(ai.Values),
            users.Values.OrderBy(message => message.At).ToArray(),
            taskKey,
            trigger,
            requestedAt,
            taskProfileId,
            taskProfileDisplayName,
            isDismissed,
            isCompanionThinking);
    }

    private static CompanionAiTimelineEntry[] CollapseVisibleFaults(
        IEnumerable<CompanionAiTimelineEntry> messages)
    {
        var materialized = messages.ToArray();
        var latestFaultIds = materialized
            .Where(message => message.Kind == "fault")
            .GroupBy(message => message.Text.Trim(), StringComparer.Ordinal)
            .Select(group => group.OrderByDescending(message => message.At).First().ArtifactId)
            .ToHashSet(StringComparer.Ordinal);
        var ordered = materialized
            .Where(message => message.Kind != "fault" || latestFaultIds.Contains(message.ArtifactId))
            .OrderBy(message => message.At)
            .ToArray();
        var collapsed = new List<CompanionAiTimelineEntry>();
        var faultRun = new List<CompanionAiTimelineEntry>();
        foreach (var message in ordered)
        {
            if (message.Kind == "fault")
            {
                faultRun.Add(message);
                continue;
            }
            FlushFaultRun();
            collapsed.Add(message);
        }
        FlushFaultRun();
        return collapsed.ToArray();

        void FlushFaultRun()
        {
            if (faultRun.Count == 0) return;
            if (faultRun.Count == 1)
            {
                collapsed.Add(faultRun[0]);
                faultRun.Clear();
                return;
            }
            var latest = faultRun[^1];
            var text = string.Join("\n\n", faultRun.Select(message =>
                $"{message.At.ToLocalTime().ToString("HH:mm:ss", CultureInfo.InvariantCulture)} · {message.Text.Trim()}"));
            collapsed.Add(new CompanionAiTimelineEntry(
                $"fault-group-{latest.ArtifactId}", "fault", latest.At, text,
                faultRun[0].StartedAt, latest.CompletedAt));
            faultRun.Clear();
        }
    }

    private static void RemoveActionPending(Dictionary<string, CompanionAiTimelineEntry> ai)
    {
        foreach (var id in ai.Where(pair => pair.Value.Kind == "action_pending").Select(pair => pair.Key).ToArray())
            ai.Remove(id);
    }

    private static void ReadProjection(
        JsonElement payload,
        Dictionary<string, CompanionAiTimelineEntry> ai,
        Dictionary<string, CompanionTimelineEntry> users,
        ref DateTimeOffset? scheduledFor,
        ref DateTimeOffset? autoSubmit,
        ref DateTimeOffset? m1Deadline,
        ref DateTimeOffset? h0LockedAt,
        ref string? taskKey,
        ref string? trigger,
        ref DateTimeOffset? requestedAt,
        ref string? taskProfileId,
        ref string? taskProfileDisplayName,
        ref bool isCompanionThinking)
    {
        taskKey = ReadNestedString(payload, "cycle", "task_key") ?? taskKey;
        trigger = ReadNestedString(payload, "cycle", "trigger") ?? trigger;
        requestedAt = ReadDate(ReadNestedString(payload, "cycle", "requested_at")) ?? requestedAt;
        taskProfileId = ReadNestedString(payload, "cycle", "task_profile_id") ?? taskProfileId;
        taskProfileDisplayName = ReadTaskProfileDisplayName(payload) ?? taskProfileDisplayName;
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
                    { } value when IsPublishedKind(value) => value,
                    _ => null,
                };
                if (kind is null) continue;
                var at = ReadDate(ReadString(message, "at")) ?? DateTimeOffset.MinValue;
                var started = kind == "m1" ? projectedM1StartedAt : kind == "m2" ? projectedM2StartedAt : at;
                var completed = kind == "m1" ? projectedM1CompletedAt : kind == "m2" ? projectedM2CompletedAt : at;
                UpsertAi(ai, ReadString(message, "artifact_id"), kind, at, ReadPublishedText(message), started ?? at, completed ?? at,
                    ReadPublishedParts(message));
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
                if (string.IsNullOrWhiteSpace(id)) continue;
                var state = ReadString(stream, "state");
                var at = ReadDate(ReadString(stream, "created_at")) ?? DateTimeOffset.MinValue;
                if (state == "streaming" && string.IsNullOrWhiteSpace(text))
                {
                    isCompanionThinking = true;
                    continue;
                }
                if (string.IsNullOrWhiteSpace(text)) continue;
                isCompanionThinking = false;
                if (state == "failed") text += "\n\n（未完成）";
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
        DateTimeOffset? completed,
        IReadOnlyList<CompanionMessagePart>? parts = null)
    {
        if (string.IsNullOrWhiteSpace(text)) return;
        var id = artifactId ?? $"{kind}-{at:O}";
        if (ai.TryGetValue(id, out var existing))
        {
            started = existing.StartedAt ?? started;
            completed = existing.CompletedAt ?? completed;
            parts ??= existing.Parts;
        }
        ai[id] = new CompanionAiTimelineEntry(id, kind, at, text, started, completed, parts);
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

    private static string? ReadPublishedText(JsonElement element) =>
        ReadNestedString(element, "message", "text_projection") ?? ReadString(element, "text");

    private static bool IsPublishedKind(string kind) => kind is
        "m0" or "m1" or "m2" or "ai_chat" or "chat" or "premarket" or "premarket_chat"
        or "judgment_revision" or "system_fault" or "fault" or "outcome" or "reflection"
        or "legacy_message" or "legacy_model" or "legacy_synthesis" or "history";

    private static string? ReadPublishedText(JsonElement element, string legacyProperty) =>
        ReadNestedString(element, "message", "text_projection") ?? ReadString(element, legacyProperty);

    private static CompanionMessagePart[]? ReadPublishedParts(JsonElement element)
    {
        if (!element.TryGetProperty("message", out var message)
            || message.ValueKind != JsonValueKind.Object
            || ReadString(message, "contract") != "companion-published-message/v2"
            || !message.TryGetProperty("parts", out var parts)
            || parts.ValueKind != JsonValueKind.Array) return null;
        return parts.EnumerateArray().Select(part => new CompanionMessagePart(
            ReadString(part, "kind") ?? "speech",
            ReadString(part, "text") ?? ReadString(part, "markdown") ?? string.Empty,
            ReadString(part, "source_title"),
            ReadString(part, "source_url"))).Where(part => !string.IsNullOrWhiteSpace(part.Text)).ToArray();
    }

    private static string? ReadCycleString(JsonElement payload, string property) =>
        ReadNestedString(payload, "cycle", property) ?? ReadString(payload, property);

    private static string? ReadTaskProfileDisplayName(JsonElement payload)
    {
        var raw = ReadCycleString(payload, "task_profile_json");
        if (string.IsNullOrWhiteSpace(raw)) return null;
        try
        {
            using var document = JsonDocument.Parse(raw);
            return ReadString(document.RootElement, "display_name");
        }
        catch (JsonException) { return null; }
    }

    private static DateTimeOffset? ReadDate(string? value) =>
        DateTimeOffset.TryParse(value, CultureInfo.InvariantCulture, DateTimeStyles.RoundtripKind, out var result) ? result : null;

    private sealed record CompanionEvent(string CycleId, string Type, DateTimeOffset At, string? TaskKey, JsonElement Payload);
}
