using AIDecisionCenter.App.Services;

namespace AIDecisionCenter.Tests;

public sealed class CompanionDraftStoreTests : IDisposable
{
    private readonly string _directory = Path.Combine(Path.GetTempPath(), $"ai-decision-drafts-{Guid.NewGuid():N}");

    [Fact]
    public void SavesAndRestoresIndependentDraftsByCycle()
    {
        var paths = new AppPaths(_directory);
        var drafts = new Dictionary<string, string>
        {
            ["cycle-a"] = "机器人板块",
            ["cycle-b"] = "有色金属",
        };

        CompanionDraftStore.Save(paths, drafts);
        var loaded = CompanionDraftStore.Load(paths);

        Assert.Equal("机器人板块", loaded["cycle-a"]);
        Assert.Equal("有色金属", loaded["cycle-b"]);
    }

    public void Dispose()
    {
        if (Directory.Exists(_directory)) Directory.Delete(_directory, true);
    }
}
