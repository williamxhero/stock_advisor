namespace AIDecisionCenter.App.Services;

public sealed class AppPaths
{
    public AppPaths(string? dataDirectory = null)
    {
        DataDirectory = dataDirectory ?? Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
            "AIDecisionCenter");
        SettingsPath = Path.Combine(DataDirectory, "appsettings.json");
        DatabasePath = Path.Combine(DataDirectory, "decision-center.db");
        OAuthClientPath = Path.Combine(DataDirectory, "oauth-client.json");
        TokenDirectory = Path.Combine(DataDirectory, "tokens");
    }

    public string DataDirectory { get; }

    public string SettingsPath { get; }

    public string DatabasePath { get; }

    public string OAuthClientPath { get; }

    public string TokenDirectory { get; }

    public void EnsureDirectories()
    {
        Directory.CreateDirectory(DataDirectory);
        Directory.CreateDirectory(TokenDirectory);
    }
}
