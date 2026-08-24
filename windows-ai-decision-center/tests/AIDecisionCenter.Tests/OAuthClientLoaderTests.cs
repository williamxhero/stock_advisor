using AIDecisionCenter.App.Services;

namespace AIDecisionCenter.Tests;

public sealed class OAuthClientLoaderTests : IDisposable
{
    private readonly string _directory = Path.Combine(
        Path.GetTempPath(),
        "AIDecisionCenter.OAuthTests",
        Guid.NewGuid().ToString("N"));

    [Fact]
    public void RejectsWebApplicationCredentialsBeforeAuthorizationStarts()
    {
        Directory.CreateDirectory(_directory);
        var path = Path.Combine(_directory, "oauth-client.json");
        File.WriteAllText(path, """
            {
              "web": {
                "client_id": "example.apps.googleusercontent.com",
                "client_secret": "test-only",
                "redirect_uris": ["https://developers.google.com/oauthplayground"]
              }
            }
            """);

        var exception = Assert.Throws<InvalidDataException>(() => OAuthClientLoader.Load(path));

        Assert.Contains("Desktop app", exception.Message, StringComparison.Ordinal);
    }

    [Fact]
    public void LoadsInstalledApplicationCredentials()
    {
        Directory.CreateDirectory(_directory);
        var path = Path.Combine(_directory, "oauth-client.json");
        File.WriteAllText(path, """
            {
              "installed": {
                "client_id": "example.apps.googleusercontent.com",
                "client_secret": "test-only"
              }
            }
            """);

        var result = OAuthClientLoader.Load(path);

        Assert.Equal("example.apps.googleusercontent.com", result.ClientId);
        Assert.Equal("test-only", result.ClientSecret);
    }

    [Fact]
    public void GmailSourceReportsInvalidConfigurationWithoutCrashing()
    {
        var paths = new AppPaths(_directory);
        paths.EnsureDirectories();
        File.WriteAllText(paths.OAuthClientPath, """
            {
              "web": {
                "client_id": "example.apps.googleusercontent.com",
                "client_secret": "test-only",
                "redirect_uris": ["https://developers.google.com/oauthplayground"]
              }
            }
            """);
        using var source = new GmailMessageSource(paths);

        var error = source.ConfigurationError;

        Assert.False(source.IsConfigured);
        Assert.Contains("Desktop app", error, StringComparison.Ordinal);
    }

    public void Dispose()
    {
        if (Directory.Exists(_directory))
        {
            Directory.Delete(_directory, recursive: true);
        }
    }
}
