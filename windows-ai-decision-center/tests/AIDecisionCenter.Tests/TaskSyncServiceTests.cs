using AIDecisionCenter.App.Services;
using AIDecisionCenter.Core.Models;

namespace AIDecisionCenter.Tests;

public sealed class TaskSyncServiceTests : IDisposable
{
    private readonly string _directory = Path.Combine(Path.GetTempPath(), "AIDecisionCenter.Tests", Guid.NewGuid().ToString("N"));

    [Fact]
    public async Task PassesStoredMessageIdsSoSourceOnlyDownloadsUnknownHistory()
    {
        var store = new SqliteTaskMessageStore(new AppPaths(_directory));
        await store.InitializeAsync();
        var existingParsed = new ParsedTaskSubject("A股", new TimeOnly(9, 0), "盘前机会发现", new DateOnly(2026, 8, 23));
        await store.AddAsync(
            new IncomingTaskMessage("existing-id", "existing", "existing body", DateTimeOffset.UtcNow),
            existingParsed);
        var source = new FakeMessageSource(
            new IncomingTaskMessage(
                "new-id",
                "[ChatGPTTask][A股][14:30] 操作决策 2026-08-24",
                "new body",
                DateTimeOffset.UtcNow));
        var service = new TaskSyncService(source, store);

        var added = await service.SyncAsync(new AppSettings());

        Assert.Contains("existing-id", source.KnownIdsReceived);
        Assert.Single(added);
        Assert.Equal(2, (await store.GetAllAsync()).Count);
    }

    [Fact]
    public void DefaultGmailQueryIncludesAllTaskHistory()
    {
        Assert.Equal("subject:\"[ChatGPTTask]\"", new GmailSettings().Query);
    }

    public void Dispose()
    {
        if (Directory.Exists(_directory))
        {
            Directory.Delete(_directory, recursive: true);
        }
    }

    private sealed class FakeMessageSource(params IncomingTaskMessage[] messages) : IGmailMessageSource
    {
        public IReadOnlySet<string> KnownIdsReceived { get; private set; } = new HashSet<string>();

        public bool IsConfigured => true;

        public bool IsAuthorized => true;

        public string? ConfigurationError => null;

        public Task<IReadOnlyList<IncomingTaskMessage>> FetchAsync(
            AppSettings settings,
            IReadOnlySet<string> knownExternalIds,
            CancellationToken cancellationToken = default)
        {
            KnownIdsReceived = knownExternalIds;
            return Task.FromResult<IReadOnlyList<IncomingTaskMessage>>(messages);
        }
    }
}
