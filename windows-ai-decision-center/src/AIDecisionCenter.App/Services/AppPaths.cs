namespace AIDecisionCenter.App.Services;

public sealed class AppPaths
{
    public AppPaths(string? dataDirectory = null)
    {
        DataDirectory = dataDirectory ?? Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
            "AIDecisionCenter");
        SettingsPath = Path.Combine(DataDirectory, "appsettings.json");
        WindowStatePath = Path.Combine(DataDirectory, "window-state.json");
        CompanionDraftsPath = Path.Combine(DataDirectory, "companion-drafts.json");
        DatabasePath = Path.Combine(DataDirectory, "decision-center.db");
        InboxRoot = Path.Combine(DataDirectory, "inbox");
        PendingDirectory = Path.Combine(InboxRoot, "pending");
        ProcessingDirectory = Path.Combine(InboxRoot, "processing");
        ProcessedDirectory = Path.Combine(InboxRoot, "processed");
        DeadLetterDirectory = Path.Combine(InboxRoot, "dead-letter");
        CompanionExchangeRoot = Path.Combine(DataDirectory, "exchange");
        CompanionToRuntimePendingDirectory = Path.Combine(CompanionExchangeRoot, "to-runtime", "pending");
        CompanionToClientPendingDirectory = Path.Combine(CompanionExchangeRoot, "to-client", "pending");
        CompanionToClientProcessedDirectory = Path.Combine(CompanionExchangeRoot, "to-client", "processed");
        CompanionAudioDirectory = Path.Combine(DataDirectory, "audio");
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
