using AITradingCompanion.Desktop.Services;
using Microsoft.Data.Sqlite;

namespace AITradingCompanion.Tests;

public sealed class SqliteTaskMessageStoreTests : IDisposable
{
    private readonly string _directory = Path.Combine(Path.GetTempPath(), "AITradingCompanion.Tests", Guid.NewGuid().ToString("N"));

    [Fact]
    public async Task StoresMessageOnceAndPersistsReviewState()
    {
        var store = new SqliteTaskMessageStore(new AppPaths(_directory));
        await store.InitializeAsync();
        var incoming = TestMessageFactory.CreateIncoming();

        var first = await store.AddAsync(incoming);
        var duplicate = await store.AddAsync(incoming);
        Assert.NotNull(first);
        Assert.Null(duplicate);

        await store.SetReadAsync(first.Id, true);
        await store.SetStarredAsync(first.Id, true);
        await store.SetArchivedAsync(first.Id, true);
        await store.SaveNoteAsync(first.Id, "等待次日确认");
        var saved = Assert.Single(await store.GetAllAsync());

        Assert.True(saved.IsRead);
        Assert.True(saved.IsStarred);
        Assert.True(saved.IsArchived);
        Assert.Equal("等待次日确认", saved.Note);
    }

    [Fact]
    public async Task RejectsDuplicateIdentifierWithDifferentContent()
    {
        var store = new SqliteTaskMessageStore(new AppPaths(_directory));
        await store.InitializeAsync();
        var original = TestMessageFactory.CreateIncoming();
        var conflicting = original with { ContentSha256 = new string('f', 64) };

        await store.AddAsync(original);

        var exception = await Assert.ThrowsAsync<InvalidDataException>(() => store.AddAsync(conflicting));
        Assert.Contains("消息标识冲突", exception.Message, StringComparison.Ordinal);
    }

    [Fact]
    public async Task RemovesOnlyRequestedGatewayCycleCacheRows()
    {
        var store = new SqliteTaskMessageStore(new AppPaths(_directory));
        await store.InitializeAsync();
        var gatewayOne = TestMessageFactory.CreateIncoming() with
        {
            ExternalId = "gateway:cycle-1",
            Source = "gateway",
            SourceRunId = "cycle-1",
            ContentSha256 = "cycle-1"
        };
        var gatewayTwo = gatewayOne with
        {
            ExternalId = "gateway:cycle-2",
            SourceRunId = "cycle-2",
            ContentSha256 = "cycle-2"
        };
        var local = gatewayOne with
        {
            ExternalId = "local:cycle-1",
            Source = "stock_advisor",
            ContentSha256 = "local-cycle-1"
        };
        await store.AddAsync(gatewayOne);
        await store.AddAsync(gatewayTwo);
        await store.AddAsync(local);

        var removed = await store.RemoveGatewayCyclesAsync(["cycle-1"]);
        var remaining = await store.GetAllAsync();

        Assert.Equal(1, removed);
        Assert.Equal(2, remaining.Count);
        Assert.Contains(remaining, message => message.SourceRunId == "cycle-2");
        Assert.Contains(remaining, message => message.Source == "stock_advisor" && message.SourceRunId == "cycle-1");
    }

    [Fact]
    public async Task MigratesLegacyGmailRowsWithoutDeletingHistory()
    {
        var paths = new AppPaths(_directory);
        paths.EnsureDirectories();
        await using (var connection = new SqliteConnection($"Data Source={paths.DatabasePath}"))
        {
            await connection.OpenAsync();
            var command = connection.CreateCommand();
            command.CommandText = """
                CREATE TABLE task_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    external_id TEXT NOT NULL UNIQUE,
                    project TEXT NOT NULL,
                    slot TEXT NOT NULL,
                    task_type TEXT NOT NULL,
                    scheduled_date TEXT NOT NULL,
                    received_at TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    body_markdown TEXT NOT NULL,
                    is_read INTEGER NOT NULL DEFAULT 0
                );
                INSERT INTO task_messages
                    (external_id, project, slot, task_type, scheduled_date, received_at, subject, body_markdown, is_read)
                VALUES ('gmail-1', 'A股', '09:00', '盘前机会发现', '2026-08-24', '2026-08-24T01:01:00Z', 'legacy', 'legacy body', 1);
                """;
            await command.ExecuteNonQueryAsync();
        }

        var store = new SqliteTaskMessageStore(paths);
        await store.InitializeAsync();
        var migrated = Assert.Single(await store.GetAllAsync());

        Assert.Equal("gmail-legacy", migrated.Source);
        Assert.True(migrated.IsRead);
        Assert.Equal("legacy body", migrated.BodyMarkdown);
        Assert.True(File.Exists(paths.DatabasePath + ".pre-local-inbox-v1.bak"));
    }

    public void Dispose()
    {
        SqliteConnection.ClearAllPools();
        if (Directory.Exists(_directory)) Directory.Delete(_directory, recursive: true);
    }
}
