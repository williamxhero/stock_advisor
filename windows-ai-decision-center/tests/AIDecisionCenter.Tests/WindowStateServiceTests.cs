using AIDecisionCenter.App.Services;

namespace AIDecisionCenter.Tests;

public sealed class WindowStateServiceTests : IDisposable
{
    private readonly string _directory = Path.Combine(Path.GetTempPath(), "AIDecisionCenter.WindowTests", Guid.NewGuid().ToString("N"));

    [Fact]
    public void SavesAndLoadsLastWindowSize()
    {
        var paths = new AppPaths(_directory);

        WindowStateService.Save(paths, 1024, 680);
        var restored = WindowStateService.Load(paths);

        Assert.NotNull(restored);
        Assert.Equal(1024, restored.Width);
        Assert.Equal(680, restored.Height);
    }

    [Fact]
    public void SavesAndLoadsLastWindowPosition()
    {
        var paths = new AppPaths(_directory);

        WindowStateService.Save(paths, 1024, 680, 140, 90);
        var restored = WindowStateService.Load(paths);

        Assert.NotNull(restored);
        Assert.Equal(140, restored.Left);
        Assert.Equal(90, restored.Top);
    }

    [Fact]
    public void IgnoresCorruptWindowState()
    {
        var paths = new AppPaths(_directory);
        paths.EnsureDirectories();
        File.WriteAllText(paths.WindowStatePath, "not-json");

        Assert.Null(WindowStateService.Load(paths));
    }

    public void Dispose()
    {
        if (Directory.Exists(_directory))
        {
            Directory.Delete(_directory, recursive: true);
        }
        GC.SuppressFinalize(this);
    }
}
