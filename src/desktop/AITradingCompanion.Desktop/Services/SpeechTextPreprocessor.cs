namespace AITradingCompanion.Desktop.Services;

public static class SpeechTextPreprocessor
{
    private static readonly string[] MetadataPrefixes =
    [
        "计划节点", "计划时间", "实际执行", "执行时间",
        "protocol", "protocol id", "run id", "registry", "registry id",
        "task key", "task_key", "scheduled_for", "completed_at"
    ];

    public static string Prepare(string? text)
    {
        if (string.IsNullOrWhiteSpace(text))
        {
            return string.Empty;
        }

        var lines = text.Replace("\r\n", "\n", StringComparison.Ordinal).Split('\n');
        var index = 0;
        SkipBlankLines(lines, ref index);
        if (index >= lines.Length)
        {
            return string.Empty;
        }

        // A report title may precede the metadata. Only one non-metadata title line
        // is allowed so ordinary prose containing a later label is not discarded.
        if (!IsMetadataLine(lines[index]))
        {
            index++;
            SkipBlankLines(lines, ref index);
        }

        var metadataCount = 0;
        while (index < lines.Length && IsMetadataLine(lines[index]))
        {
            metadataCount++;
            index++;
            SkipBlankLines(lines, ref index);
        }

        if (metadataCount < 2)
        {
            return text.Trim();
        }

        return string.Join(Environment.NewLine, lines[index..]).Trim();
    }

    private static bool IsMetadataLine(string line)
    {
        var normalized = line.TrimStart();
        return MetadataPrefixes.Any(prefix =>
            normalized.StartsWith(prefix, StringComparison.OrdinalIgnoreCase) &&
            HasFieldSeparator(normalized, prefix.Length));
    }

    private static bool HasFieldSeparator(string value, int prefixLength)
    {
        if (value.Length == prefixLength)
        {
            return true;
        }
        var remainder = value[prefixLength..].TrimStart();
        return remainder.StartsWith(':') || remainder.StartsWith('：');
    }

    private static void SkipBlankLines(string[] lines, ref int index)
    {
        while (index < lines.Length && string.IsNullOrWhiteSpace(lines[index]))
        {
            index++;
        }
    }
}
