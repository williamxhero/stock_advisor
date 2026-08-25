using AITradingCompanion.Desktop.Services;

namespace AITradingCompanion.Tests;

public sealed class LocalInboxServiceTests : IDisposable
{
    private readonly string _directory = Path.Combine(Path.GetTempPath(), "AITradingCompanion.Tests", Guid.NewGuid().ToString("N"));

    [Fact]
    public async Task ImportsPendingMessageAndMovesItToProcessed()
    {
        var paths = new AppPaths(_directory);
        var store = new SqliteTaskMessageStore(paths);
        await store.InitializeAsync();
        using var inbox = new LocalInboxService(paths, store, new AppSettings());
        var runId = Guid.NewGuid().ToString();
        await File.WriteAllTextAsync(Path.Combine(paths.PendingDirectory, $"{runId}.json"), TestMessageFactory.CreateEnvelope(runId));

        var batch = await inbox.ImportAvailableAsync();

        Assert.Single(batch.Added);
        Assert.Empty(Directory.EnumerateFiles(paths.PendingDirectory, "*.json"));
        Assert.Single(Directory.EnumerateFiles(paths.ProcessedDirectory, "*.json", SearchOption.AllDirectories));
    }

    [Fact]
    public async Task DuplicateIsIdempotentAndDoesNotReturnSecondNotification()
    {
        var paths = new AppPaths(_directory);
        var store = new SqliteTaskMessageStore(paths);
        await store.InitializeAsync();
        using var inbox = new LocalInboxService(paths, store, new AppSettings());
        var runId = Guid.NewGuid().ToString();
        var json = TestMessageFactory.CreateEnvelope(runId);
        var pending = Path.Combine(paths.PendingDirectory, $"{runId}.json");
        await File.WriteAllTextAsync(pending, json);
        await inbox.ImportAvailableAsync();
        await File.WriteAllTextAsync(pending, json);

        var duplicate = await inbox.ImportAvailableAsync();

        Assert.Empty(duplicate.Added);
        Assert.Equal(1, duplicate.DuplicateCount);
        Assert.Single(await store.GetAllAsync());
    }

    [Fact]
    public async Task InvalidMessageMovesToDeadLetterWithReason()
    {
        var paths = new AppPaths(_directory);
        var store = new SqliteTaskMessageStore(paths);
        await store.InitializeAsync();
        using var inbox = new LocalInboxService(paths, store, new AppSettings());
        await File.WriteAllTextAsync(Path.Combine(paths.PendingDirectory, "broken.json"), "{broken");

        var batch = await inbox.ImportAvailableAsync();

        Assert.Equal(1, batch.DeadLetterCount);
        Assert.Single(Directory.EnumerateFiles(paths.DeadLetterDirectory, "*.json"));
        Assert.Single(Directory.EnumerateFiles(paths.DeadLetterDirectory, "*.error.txt"));
    }

    [Fact]
    public async Task ReconcilesProcessedMessageMissingFromDatabase()
    {
        var paths = new AppPaths(_directory);
        paths.EnsureDirectories();
        var store = new SqliteTaskMessageStore(paths);
        await store.InitializeAsync();
        using var inbox = new LocalInboxService(paths, store, new AppSettings());
        var runId = Guid.NewGuid().ToString();
        var archive = Path.Combine(paths.ProcessedDirectory, "2026-08-25");
        Directory.CreateDirectory(archive);
        var processed = Path.Combine(archive, $"{runId}.json");
        await File.WriteAllTextAsync(processed, TestMessageFactory.CreateEnvelope(runId));

        var batch = await inbox.ImportAvailableAsync();

        Assert.Single(batch.Added);
        Assert.Equal(1, batch.RecoveredCount);
        Assert.Single(await store.GetAllAsync());
        Assert.True(File.Exists(processed));
    }

    public void Dispose()
    {
        if (Directory.Exists(_directory)) Directory.Delete(_directory, recursive: true);
    }
}
