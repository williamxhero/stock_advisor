namespace AIDecisionCenter.Core.Models;

public sealed record TaskMessage(
    long Id,
    string ExternalId,
    string Project,
    TimeOnly Slot,
    string TaskType,
    DateOnly ScheduledDate,
    DateTimeOffset ReceivedAt,
    string Subject,
    string BodyMarkdown,
    bool IsRead);
