using AITradingCompanion.Core.Models;

namespace AITradingCompanion.Desktop.Services;

public interface ITaskMessageStore
{
    Task InitializeAsync(CancellationToken cancellationToken = default);

    Task<TaskMessage?> AddAsync(IncomingTaskMessage incoming, CancellationToken cancellationToken = default);

    Task<IReadOnlyList<TaskMessage>> GetForDateAsync(DateOnly scheduledDate, CancellationToken cancellationToken = default);

    Task<IReadOnlyList<TaskMessage>> GetAllAsync(CancellationToken cancellationToken = default);

    Task SetReadAsync(long id, bool isRead, CancellationToken cancellationToken = default);

    Task SetStarredAsync(long id, bool isStarred, CancellationToken cancellationToken = default);

    Task SetArchivedAsync(long id, bool isArchived, CancellationToken cancellationToken = default);

    Task<int> RemoveGatewayCyclesAsync(IEnumerable<string> cycleIds, CancellationToken cancellationToken = default);

    Task SaveNoteAsync(long id, string note, CancellationToken cancellationToken = default);
}
