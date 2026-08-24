using AIDecisionCenter.App.Services;

namespace AIDecisionCenter.Tests;

public sealed class SettingsServiceTests : IDisposable
{
    private readonly string _directory = Path.Combine(Path.GetTempPath(), "AIDecisionCenter.Tests", Guid.NewGuid().ToString("N"));

    [Fact]
    public async Task MigratesLegacyRecentOnlyQueryToAllHistoryQuery()
    {
        var paths = new AppPaths(_directory);
        paths.EnsureDirectories();
        await File.WriteAllTextAsync(paths.SettingsPath, """
            {
              "Gmail": {
                "Query": "subject:\"[ChatGPTTask]\" newer_than:14d",
                "MaxMessagesPerSync": 50
              },
              "Polling": {
                "ActiveSeconds": 30
              }
            }
            """);

        var settings = await new SettingsService(paths).LoadAsync();

        Assert.Equal("subject:\"[ChatGPTTask]\"", settings.Gmail.Query);
        Assert.DoesNotContain("newer_than", await File.ReadAllTextAsync(paths.SettingsPath));
    }

    public void Dispose()
    {
        if (Directory.Exists(_directory))
        {
            Directory.Delete(_directory, recursive: true);
        }
    }
}
