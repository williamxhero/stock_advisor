using AIDecisionCenter.Core.Models;

namespace AIDecisionCenter.App.Services;

public interface ITaskMessageStore
{
    Task InitializeAsync(CancellationToken cancellationToken = default);

    Task<bool> AddAsync(IncomingTaskMessage incoming, ParsedTaskSubject parsed, CancellationToken cancellationToken = default);

    Task<IReadOnlyList<TaskMessage>> GetForDateAsync(DateOnly scheduledDate, CancellationToken cancellationToken = default);

    Task<IReadOnlyList<TaskMessage>> GetAllAsync(CancellationToken cancellationToken = default);

    Task<IReadOnlySet<string>> GetExternalIdsAsync(CancellationToken cancellationToken = default);

    Task MarkReadAsync(long id, CancellationToken cancellationToken = default);
}
