using System.Text.Json;
using System.Text.Json.Serialization;

namespace AIDecisionCenter.App.Services;

public sealed class SettingsService
{
    private const string LegacyRecentOnlyQuery = "subject:\"[ChatGPTTask]\" newer_than:14d";

    private static readonly JsonSerializerOptions JsonOptions = new()
    {
        WriteIndented = true,
        PropertyNameCaseInsensitive = true,
        Converters = { new JsonStringEnumConverter() }
    };

    private readonly AppPaths _paths;

    public SettingsService(AppPaths paths)
    {
        _paths = paths;
    }

    public async Task<AppSettings> LoadAsync(CancellationToken cancellationToken = default)
    {
        _paths.EnsureDirectories();
        if (!File.Exists(_paths.SettingsPath))
        {
            var defaults = new AppSettings();
            await SaveAsync(defaults, cancellationToken).ConfigureAwait(false);
            return defaults;
        }

        AppSettings settings;
        await using (var stream = File.OpenRead(_paths.SettingsPath))
        {
            settings = await JsonSerializer.DeserializeAsync<AppSettings>(stream, JsonOptions, cancellationToken).ConfigureAwait(false)
                ?? new AppSettings();
        }
        if (!string.Equals(settings.Gmail.Query, LegacyRecentOnlyQuery, StringComparison.Ordinal))
        {
            return settings;
        }

        var migrated = new AppSettings
        {
            Gmail = new GmailSettings
            {
                Query = new GmailSettings().Query,
                MaxMessagesPerSync = settings.Gmail.MaxMessagesPerSync
            },
            Polling = settings.Polling
        };
        await SaveAsync(migrated, cancellationToken).ConfigureAwait(false);
        return migrated;
    }

    public async Task SaveAsync(AppSettings settings, CancellationToken cancellationToken = default)
    {
        _paths.EnsureDirectories();
        await using var stream = File.Create(_paths.SettingsPath);
        await JsonSerializer.SerializeAsync(stream, settings, JsonOptions, cancellationToken).ConfigureAwait(false);
    }
}
