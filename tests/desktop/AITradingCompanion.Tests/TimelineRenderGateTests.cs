using AITradingCompanion.Desktop.Services;

namespace AITradingCompanion.Tests;

public sealed class TimelineRenderGateTests
{
    [Fact]
    public void AnUnchangedTimelineDoesNotRequestAnotherRender()
    {
        var gate = new TimelineRenderGate();
        var messages = new[] { "message-1", "message-2" };

        Assert.True(gate.ShouldRender(messages));
        Assert.False(gate.ShouldRender(messages));
        Assert.True(gate.ShouldRender(["message-1", "message-3"]));
    }

    [Fact]
    public void MainWindowUsesTheGateForBothTimelines()
    {
        var root = new DirectoryInfo(AppContext.BaseDirectory);
        while (root is not null && !File.Exists(Path.Combine(root.FullName, "AITradingCompanion.sln"))) root = root.Parent;
        Assert.NotNull(root);
        var source = File.ReadAllText(Path.Combine(root.FullName!,
            "src", "desktop", "AITradingCompanion.Desktop", "Views", "MainWindow.xaml.cs"));

        Assert.Contains("_aiTimelineRenderGate.ShouldRender(AiTimelineRenderKeys(orderedMessages))", source, StringComparison.Ordinal);
        Assert.Contains("_userTimelineRenderGate.ShouldRender(UserTimelineRenderKeys(messages))", source, StringComparison.Ordinal);
        Assert.Contains("_draftSaveTimer.Start();", source, StringComparison.Ordinal);
    }
}
