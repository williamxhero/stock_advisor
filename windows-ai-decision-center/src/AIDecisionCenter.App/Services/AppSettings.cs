namespace AIDecisionCenter.App.Services;

public sealed class AppSettings
{
    public GmailSettings Gmail { get; init; } = new();

    public PollingSettings Polling { get; init; } = new();
}

public sealed class GmailSettings
{
    public string Query { get; init; } = "subject:\"[ChatGPTTask]\"";

    public int MaxMessagesPerSync { get; init; } = 50;
}

public sealed class PollingSettings
{
    public int ActiveSeconds { get; init; } = 30;

    public int NodeTimeoutMinutes { get; init; } = 20;

    public int IdleSeconds { get; init; } = 300;

    public TimeOnly ActiveFrom { get; init; } = new(7, 50);

    public TimeOnly ActiveUntil { get; init; } = new(15, 40);
}
