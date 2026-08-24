namespace AIDecisionCenter.Core.Models;

public sealed record ParsedTaskSubject(
    string Project,
    TimeOnly Slot,
    string TaskType,
    DateOnly ScheduledDate);
