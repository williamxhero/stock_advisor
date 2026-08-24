using System.Globalization;
using System.Text.RegularExpressions;
using AIDecisionCenter.Core.Models;

namespace AIDecisionCenter.Core.Parsing;

public static partial class TaskSubjectParser
{
    public static bool TryParse(string subject, out ParsedTaskSubject? result)
    {
        result = null;
        var match = SubjectPattern().Match(subject.Trim());
        if (!match.Success ||
            !TimeOnly.TryParseExact(match.Groups["slot"].Value, "HH:mm", CultureInfo.InvariantCulture, DateTimeStyles.None, out var slot) ||
            !DateOnly.TryParseExact(match.Groups["date"].Value, "yyyy-MM-dd", CultureInfo.InvariantCulture, DateTimeStyles.None, out var date))
        {
            return false;
        }

        result = new ParsedTaskSubject(
            match.Groups["project"].Value.Trim(),
            slot,
            match.Groups["type"].Value.Trim(),
            date);
        return true;
    }

    [GeneratedRegex(@"^\[ChatGPTTask\]\[(?<project>[^\]]+)\]\[(?<slot>\d{2}:\d{2})\]\s*(?<type>.+?)\s+(?<date>\d{4}-\d{2}-\d{2})$", RegexOptions.CultureInvariant)]
    private static partial Regex SubjectPattern();
}
