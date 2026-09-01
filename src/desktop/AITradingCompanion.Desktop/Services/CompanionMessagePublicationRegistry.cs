using System.Text.Json;

namespace AITradingCompanion.Desktop.Services;

internal static class CompanionMessagePublicationRegistry
{
    private sealed record Registry(
        string Contract,
        string[] PublishedEventTypes,
        string[] PublishedKinds,
        string[] LegacyReadKinds);

    private static readonly Lazy<(HashSet<string> Published, HashSet<string> Legacy)> Kinds = new(LoadKinds);
    private static readonly JsonSerializerOptions JsonOptions = new() { PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower };

    public static bool CanReadKind(string kind) =>
        Kinds.Value.Published.Contains(kind) || Kinds.Value.Legacy.Contains(kind);

    private static (HashSet<string>, HashSet<string>) LoadKinds()
    {
        var path = Path.Combine(AppContext.BaseDirectory, "resources", "contracts", "companion-message-publication-registry-v2.json");
        var registry = JsonSerializer.Deserialize<Registry>(File.ReadAllText(path), JsonOptions)
            ?? throw new InvalidDataException("Companion message publication registry is empty.");
        if (registry.Contract != "companion-message-publication-registry/v2")
            throw new InvalidDataException("Companion message publication registry contract is invalid.");
        return (
            registry.PublishedKinds.ToHashSet(StringComparer.Ordinal),
            registry.LegacyReadKinds.ToHashSet(StringComparer.Ordinal));
    }
}
