namespace AITradingCompanion.Desktop.Services;

public sealed class AppSettings
{
    public InboxSettings Inbox { get; init; } = new();

    public DisplaySettings Display { get; init; } = new();
}

public sealed class InboxSettings
{
    public int ScanIntervalSeconds { get; init; } = 30;

    public int DebounceMilliseconds { get; init; } = 300;

    public int MaxMessageBytes { get; init; } = 10 * 1024 * 1024;
}

public sealed class DisplaySettings
{
    public int NodeTimeoutMinutes { get; init; } = 20;
}
