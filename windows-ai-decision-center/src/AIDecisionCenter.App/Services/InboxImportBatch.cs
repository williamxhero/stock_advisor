using AIDecisionCenter.Core.Models;

namespace AIDecisionCenter.App.Services;

public sealed record InboxImportBatch(
    IReadOnlyList<TaskMessage> Added,
    int DuplicateCount,
    int DeadLetterCount)
{
    public static InboxImportBatch Empty { get; } = new([], 0, 0);
}
