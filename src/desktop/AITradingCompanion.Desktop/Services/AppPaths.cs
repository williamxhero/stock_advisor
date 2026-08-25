namespace AITradingCompanion.Desktop.Services;

public sealed class AppPaths
{
    public AppPaths(string? dataDirectory = null)
    {
        DataDirectory = dataDirectory ?? Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
            "AITradingCompanion");
        var uiDirectory = Path.Combine(DataDirectory, "ui");
        SettingsPath = Path.Combine(uiDirectory, "settings.json");
        WindowStatePath = Path.Combine(uiDirectory, "window-state.json");
        CompanionDraftsPath = Path.Combine(uiDirectory, "drafts.json");
        DatabasePath = Path.Combine(uiDirectory, "legacy-message-cache.sqlite3");
        InboxRoot = Path.Combine(DataDirectory, "legacy-inbox");
        PendingDirectory = Path.Combine(InboxRoot, "pending");
        ProcessingDirectory = Path.Combine(InboxRoot, "processing");
        ProcessedDirectory = Path.Combine(InboxRoot, "processed");
        DeadLetterDirectory = Path.Combine(InboxRoot, "dead-letter");
        CompanionExchangeRoot = Path.Combine(DataDirectory, "exchange");
        CompanionToRuntimePendingDirectory = Path.Combine(CompanionExchangeRoot, "to-runtime", "pending");
        CompanionToClientPendingDirectory = Path.Combine(CompanionExchangeRoot, "to-client", "pending");
        CompanionToClientProcessedDirectory = Path.Combine(CompanionExchangeRoot, "to-client", "processed");
        CompanionAudioDirectory = Path.Combine(uiDirectory, "audio");
    }

    public string DataDirectory { get; }

    public string SettingsPath { get; }

    public string WindowStatePath { get; }
    public string CompanionDraftsPath { get; }

    public string DatabasePath { get; }

    public string InboxRoot { get; }

    public string PendingDirectory { get; }

    public string ProcessingDirectory { get; }

    public string ProcessedDirectory { get; }

    public string DeadLetterDirectory { get; }
    public string CompanionExchangeRoot { get; }
    public string CompanionToRuntimePendingDirectory { get; }
    public string CompanionToClientPendingDirectory { get; }
    public string CompanionToClientProcessedDirectory { get; }
    public string CompanionAudioDirectory { get; }

    public void EnsureDirectories()
    {
        Directory.CreateDirectory(DataDirectory);
        Directory.CreateDirectory(Path.GetDirectoryName(SettingsPath)!);
        Directory.CreateDirectory(PendingDirectory);
        Directory.CreateDirectory(ProcessingDirectory);
        Directory.CreateDirectory(ProcessedDirectory);
        Directory.CreateDirectory(DeadLetterDirectory);
        Directory.CreateDirectory(CompanionToRuntimePendingDirectory);
        Directory.CreateDirectory(CompanionToClientPendingDirectory);
        Directory.CreateDirectory(CompanionToClientProcessedDirectory);
        Directory.CreateDirectory(CompanionAudioDirectory);
    }
}
