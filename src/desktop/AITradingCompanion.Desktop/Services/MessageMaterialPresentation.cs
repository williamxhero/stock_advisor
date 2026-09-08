using System.Text.RegularExpressions;

namespace AITradingCompanion.Desktop.Services;

internal sealed record CompactMaterialSources(int Count, string Markdown);

internal static partial class MessageMaterialPresentation
{
    private static readonly (string HostSuffix, string Title)[] PublicSourceTitles =
    [
        ("gtimg.cn", "腾讯行情数据"),
        ("eastmoney.com", "东方财富市场数据"),
        ("cls.cn", "财联社报道"),
        ("10jqka.com.cn", "同花顺报道"),
        ("sse.com.cn", "上交所资料"),
        ("szse.cn", "深交所资料"),
        ("cninfo.com.cn", "巨潮资讯资料"),
    ];

    internal static bool IsLinkOnly(CompanionMessagePart part) =>
        string.Equals(part.Kind, "material", StringComparison.Ordinal)
        && !string.IsNullOrWhiteSpace(part.SourceUrl)
        && Uri.TryCreate(part.SourceUrl, UriKind.Absolute, out _)
        && LinkOnlyMarkdown().IsMatch(part.Text.Trim());

    internal static CompactMaterialSources CompactLinkOnlySources(IEnumerable<CompanionMessagePart> parts)
    {
        var sources = parts
            .Where(IsLinkOnly)
            .Where(part => Uri.TryCreate(part.SourceUrl, UriKind.Absolute, out _))
            .GroupBy(part => part.SourceUrl!, StringComparer.OrdinalIgnoreCase)
            .Select(group => group.First())
            .Select(part => (Label: PublicLabel(part), Url: part.SourceUrl!))
            .ToArray();

        var repeatedLabels = sources
            .GroupBy(source => source.Label, StringComparer.Ordinal)
            .Where(group => group.Count() > 1)
            .ToDictionary(group => group.Key, group => group.Count(), StringComparer.Ordinal);
        var seenLabels = new Dictionary<string, int>(StringComparer.Ordinal);
        var links = sources.Select(source =>
        {
            var label = source.Label;
            if (repeatedLabels.ContainsKey(label))
            {
                seenLabels.TryGetValue(label, out var seen);
                seenLabels[label] = ++seen;
                label = $"{label} {seen}";
            }
            return $"[{EscapeLabel(label)}]({source.Url})";
        });
        return new CompactMaterialSources(sources.Length, string.Join("　", links));
    }

    private static string PublicLabel(CompanionMessagePart part)
    {
        var title = (part.SourceTitle ?? string.Empty).Trim();
        if (!Uri.TryCreate(part.SourceUrl, UriKind.Absolute, out var uri))
            return LooksInternal(title) ? "来源资料" : title;

        var publicTitle = PublicSourceTitles
            .FirstOrDefault(source => uri.Host.Equals(source.HostSuffix, StringComparison.OrdinalIgnoreCase)
                || uri.Host.EndsWith("." + source.HostSuffix, StringComparison.OrdinalIgnoreCase)).Title;
        if (string.IsNullOrWhiteSpace(publicTitle))
            publicTitle = LooksInternal(title) ? $"{uri.Host.Replace("www.", string.Empty, StringComparison.OrdinalIgnoreCase)} 资料" : title;

        if (publicTitle == "腾讯行情数据")
        {
            var symbol = IndexName(uri.Query);
            if (symbol is not null)
                return $"{publicTitle}·{symbol}";
        }
        return publicTitle;
    }

    private static string? IndexName(string query)
    {
        if (query.Contains("sh000001", StringComparison.OrdinalIgnoreCase)) return "上证指数";
        if (query.Contains("sz399001", StringComparison.OrdinalIgnoreCase)) return "深证成指";
        if (query.Contains("sz399006", StringComparison.OrdinalIgnoreCase)) return "创业板指";
        return null;
    }

    private static bool LooksInternal(string title) => MachineSourceTitle().IsMatch(title);

    private static string EscapeLabel(string label) => label.Replace("[", "〔", StringComparison.Ordinal)
        .Replace("]", "〕", StringComparison.Ordinal);

    [GeneratedRegex(@"^\s*\[[^\]]+\]\(https?://[^)]+\)\s*$", RegexOptions.IgnoreCase)]
    private static partial Regex LinkOnlyMarkdown();

    [GeneratedRegex(@"^[a-z][a-z0-9]*(?:[_+.-][a-z0-9]+)+$", RegexOptions.IgnoreCase)]
    private static partial Regex MachineSourceTitle();
}
