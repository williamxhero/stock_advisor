using AITradingCompanion.Desktop.Services;

namespace AITradingCompanion.Tests;

public sealed class SettingsServiceTests : IDisposable
{
    private readonly string _directory = Path.Combine(Path.GetTempPath(), "AITradingCompanion.Tests", Guid.NewGuid().ToString("N"));

    [Fact]
    public async Task CreatesOfflineInboxDefaultsWithoutGmailConfiguration()
    {
        var paths = new AppPaths(_directory);

        var settings = await new SettingsService(paths).LoadAsync();

        Assert.Equal(30, settings.Inbox.ScanIntervalSeconds);
        Assert.Equal(300, settings.Inbox.DebounceMilliseconds);
        Assert.Equal(20, settings.Display.NodeTimeoutMinutes);
        Assert.True(File.Exists(paths.SettingsPath));
    }

    [Fact]
    public async Task RewritesLegacyGmailSettingsToOfflineShape()
    {
        var paths = new AppPaths(_directory);
        paths.EnsureDirectories();
        await File.WriteAllTextAsync(paths.SettingsPath, "{\"Gmail\":{},\"Polling\":{}}");

        await new SettingsService(paths).LoadAsync();
        var saved = await File.ReadAllTextAsync(paths.SettingsPath);

        Assert.Contains("Inbox", saved, StringComparison.Ordinal);
        Assert.DoesNotContain("Gmail", saved, StringComparison.Ordinal);
    }

    public void Dispose()
    {
        if (Directory.Exists(_directory)) Directory.Delete(_directory, recursive: true);
    }
}
