using System.Net;
using System.Text.RegularExpressions;

namespace AIDecisionCenter.Core.Parsing;

public static partial class PlainTextSanitizer
{
    public static string FromHtml(string html)
    {
        var withLineBreaks = BreakPattern().Replace(html, "\n");
        var withoutTags = TagPattern().Replace(withLineBreaks, string.Empty);
        return WebUtility.HtmlDecode(withoutTags).Replace("\r\n", "\n", StringComparison.Ordinal).Trim();
    }

    [GeneratedRegex(@"<(br\s*/?|/p|/div|/li|/h[1-6])>", RegexOptions.IgnoreCase | RegexOptions.CultureInvariant)]
    private static partial Regex BreakPattern();

    [GeneratedRegex(@"<[^>]+>", RegexOptions.CultureInvariant)]
    private static partial Regex TagPattern();
}
