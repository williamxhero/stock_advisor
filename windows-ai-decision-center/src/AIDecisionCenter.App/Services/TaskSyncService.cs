using AIDecisionCenter.Core.Models;
using AIDecisionCenter.Core.Parsing;

namespace AIDecisionCenter.App.Services;

public sealed class TaskSyncService
{
    private readonly IGmailMessageSource _source;
    private readonly ITaskMessageStore _store;

    public TaskSyncService(IGmailMessageSource source, ITaskMessageStore store)
    {
        _source = source;
        _store = store;
    }

    public bool IsConfigured => _source.IsConfigured;

    public bool IsAuthorized => _source.IsAuthorized;

    public string? ConfigurationError => _source.ConfigurationError;

    public async Task<IReadOnlyList<TaskMessage>> SyncAsync(AppSettings settings, CancellationToken cancellationToken = default)
    {
        var added = new List<TaskMessage>();
        var knownExternalIds = await _store.GetExternalIdsAsync(cancellationToken).ConfigureAwait(false);
        foreach (var incoming in await _source.FetchAsync(settings, knownExternalIds, cancellationToken).ConfigureAwait(false))
        {
            if (!TaskSubjectParser.TryParse(incoming.Subject, out var parsed) || parsed is null)
            {
                continue;
            }

            if (await _store.AddAsync(incoming, parsed, cancellationToken).ConfigureAwait(false))
            {
                added.Add(new TaskMessage(
                    0,
                    incoming.ExternalId,
                    parsed.Project,
                    parsed.Slot,
                    parsed.TaskType,
                    parsed.ScheduledDate,
                    incoming.ReceivedAt,
                    incoming.Subject,
                    incoming.BodyMarkdown,
                    false));
            }
        }

        return added;
    }
}
