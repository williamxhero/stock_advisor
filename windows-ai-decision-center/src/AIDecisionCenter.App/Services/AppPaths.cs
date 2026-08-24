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
        InboxRoot = Path.Combine(DataDirectory, "inbox");
        PendingDirectory = Path.Combine(InboxRoot, "pending");
        ProcessingDirectory = Path.Combine(InboxRoot, "processing");
        ProcessedDirectory = Path.Combine(InboxRoot, "processed");
        DeadLetterDirectory = Path.Combine(InboxRoot, "dead-letter");
    }

    public string DataDirectory { get; }

    public string SettingsPath { get; }

    public string DatabasePath { get; }

    public string InboxRoot { get; }

    public string PendingDirectory { get; }

    public string ProcessingDirectory { get; }

    public string ProcessedDirectory { get; }

    public string DeadLetterDirectory { get; }

    public void EnsureDirectories()
    {
        Directory.CreateDirectory(DataDirectory);
        Directory.CreateDirectory(PendingDirectory);
        Directory.CreateDirectory(ProcessingDirectory);
        Directory.CreateDirectory(ProcessedDirectory);
        Directory.CreateDirectory(DeadLetterDirectory);
    }
}
