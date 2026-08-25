using AIDecisionCenter.Core.Models;

namespace AIDecisionCenter.App.Services;

public sealed record InboxImportBatch(
    IReadOnlyList<TaskMessage> Added,
    int DuplicateCount,
    int DeadLetterCount,
    int RecoveredCount,
    int ReconciliationErrorCount)
{
    public static InboxImportBatch Empty { get; } = new([], 0, 0, 0, 0);
}
