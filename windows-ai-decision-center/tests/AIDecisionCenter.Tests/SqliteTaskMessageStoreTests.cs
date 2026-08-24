using AIDecisionCenter.App.Services;
using AIDecisionCenter.Core.Models;

namespace AIDecisionCenter.Tests;

public sealed class SqliteTaskMessageStoreTests : IDisposable
{
    private readonly string _directory = Path.Combine(Path.GetTempPath(), "AIDecisionCenter.Tests", Guid.NewGuid().ToString("N"));

    [Fact]
    public async Task StoresEachGmailMessageOnlyOnce()
    {
        var store = new SqliteTaskMessageStore(new AppPaths(_directory));
        await store.InitializeAsync();
        var parsed = new ParsedTaskSubject("A股", new TimeOnly(8, 0), "盘前机会发现", new DateOnly(2026, 8, 24));
        var incoming = new IncomingTaskMessage("gmail-message-1", "subject", "body", DateTimeOffset.UtcNow);

        var first = await store.AddAsync(incoming, parsed);
        var second = await store.AddAsync(incoming, parsed);
        var rows = await store.GetForDateAsync(parsed.ScheduledDate);

        Assert.True(first);
        Assert.False(second);
        Assert.Single(rows);
        Assert.False(rows[0].IsRead);
    }

    [Fact]
    public async Task MarksMessageAsRead()
    {
        var store = new SqliteTaskMessageStore(new AppPaths(_directory));
        await store.InitializeAsync();
        var parsed = new ParsedTaskSubject("A股", new TimeOnly(9, 45), "开盘异常发现", new DateOnly(2026, 8, 24));
        await store.AddAsync(new IncomingTaskMessage("gmail-message-2", "subject", "body", DateTimeOffset.UtcNow), parsed);
        var saved = Assert.Single(await store.GetForDateAsync(parsed.ScheduledDate));

        await store.MarkReadAsync(saved.Id);
        var updated = Assert.Single(await store.GetForDateAsync(parsed.ScheduledDate));

        Assert.True(updated.IsRead);
    }

    [Fact]
    public async Task InitializeRemovesLegacyDemoMessages()
    {
        var store = new SqliteTaskMessageStore(new AppPaths(_directory));
        await store.InitializeAsync();
        var date = new DateOnly(2026, 8, 24);
        var parsed = new ParsedTaskSubject("A股", new TimeOnly(9, 0), "盘前机会发现", date);
        await store.AddAsync(
            new IncomingTaskMessage("demo-20260824-0900", "subject", "body", DateTimeOffset.UtcNow),
            parsed);
        Assert.Single(await store.GetForDateAsync(date));

        await store.InitializeAsync();

        Assert.Empty(await store.GetForDateAsync(date));
    }

    [Fact]
    public async Task ReturnsEveryMessageAsOneHistoryRecordNewestFirst()
    {
        var store = new SqliteTaskMessageStore(new AppPaths(_directory));
        await store.InitializeAsync();
        var firstParsed = new ParsedTaskSubject("A股", new TimeOnly(9, 0), "盘前机会发现", new DateOnly(2026, 8, 23));
        var secondParsed = new ParsedTaskSubject("A股", new TimeOnly(14, 30), "操作决策", new DateOnly(2026, 8, 24));
        await store.AddAsync(
            new IncomingTaskMessage("gmail-history-1", "first", "first body", new DateTimeOffset(2026, 8, 23, 1, 0, 0, TimeSpan.Zero)),
            firstParsed);
        await store.AddAsync(
            new IncomingTaskMessage("gmail-history-2", "second", "second body", new DateTimeOffset(2026, 8, 24, 6, 30, 0, TimeSpan.Zero)),
            secondParsed);
        await store.AddAsync(
            new IncomingTaskMessage("gmail-history-2", "duplicate", "duplicate body", new DateTimeOffset(2026, 8, 24, 6, 31, 0, TimeSpan.Zero)),
            secondParsed);

        var history = await store.GetAllAsync();
        var externalIds = await store.GetExternalIdsAsync();

        Assert.Equal(["gmail-history-2", "gmail-history-1"], history.Select(message => message.ExternalId));
        Assert.Equal("操作决策", history[0].TaskType);
        Assert.Equal(2, externalIds.Count);
        Assert.Contains("gmail-history-1", externalIds);
        Assert.Contains("gmail-history-2", externalIds);
    }

    public void Dispose()
    {
        if (Directory.Exists(_directory))
        {
            Directory.Delete(_directory, recursive: true);
        }
    }
}
