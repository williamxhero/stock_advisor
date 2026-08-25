using System.Text.Json;

namespace AIDecisionCenter.App.Services;

public static class CompanionDraftStore
{
    private static readonly JsonSerializerOptions Options = new() { WriteIndented = true };

    public static Dictionary<string, string> Load(AppPaths paths)
    {
        try
        {
            if (!File.Exists(paths.CompanionDraftsPath)) return new(StringComparer.Ordinal);
            return JsonSerializer.Deserialize<Dictionary<string, string>>(
                File.ReadAllText(paths.CompanionDraftsPath), Options) is { } drafts
                ? new Dictionary<string, string>(drafts, StringComparer.Ordinal)
                : new(StringComparer.Ordinal);
        }
        catch (JsonException)
        {
            return new(StringComparer.Ordinal);
        }
    }

    public static void Save(AppPaths paths, IReadOnlyDictionary<string, string> drafts)
    {
        Directory.CreateDirectory(paths.DataDirectory);
        var temporary = $"{paths.CompanionDraftsPath}.{Guid.NewGuid():N}.tmp";
        File.WriteAllText(temporary, JsonSerializer.Serialize(drafts, Options));
        File.Move(temporary, paths.CompanionDraftsPath, true);
    }
}
