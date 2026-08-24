using AIDecisionCenter.Core.Models;

namespace AIDecisionCenter.App.Services;

public interface IGmailMessageSource
{
    bool IsConfigured { get; }

    bool IsAuthorized { get; }

    string? ConfigurationError { get; }

    Task<IReadOnlyList<IncomingTaskMessage>> FetchAsync(
        AppSettings settings,
        IReadOnlySet<string> knownExternalIds,
        CancellationToken cancellationToken = default);
}
