using System.Text.Json;

namespace AITradingCompanion.Desktop.Services;

public static class CompanionDraftStore
{
    public const string ConversationDraftKey = "conversation.general";
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
        Directory.CreateDirectory(Path.GetDirectoryName(paths.CompanionDraftsPath)!);
        var temporary = $"{paths.CompanionDraftsPath}.{Guid.NewGuid():N}.tmp";
        File.WriteAllText(temporary, JsonSerializer.Serialize(drafts, Options));
        File.Move(temporary, paths.CompanionDraftsPath, true);
    }
}
