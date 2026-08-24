using System.Windows;
using System.Windows.Documents;
using System.Windows.Media;
using MediaBrush = System.Windows.Media.Brush;
using MediaBrushes = System.Windows.Media.Brushes;
using MediaColor = System.Windows.Media.Color;
using MediaFontFamily = System.Windows.Media.FontFamily;

namespace AIDecisionCenter.App.Converters;

public static class MarkdownDocumentBuilder
{
    private static readonly MediaBrush PrimaryText = new SolidColorBrush(MediaColor.FromRgb(232, 238, 248));
    private static readonly MediaBrush SecondaryText = new SolidColorBrush(MediaColor.FromRgb(166, 178, 199));
    private static readonly MediaBrush Accent = new SolidColorBrush(MediaColor.FromRgb(70, 211, 166));

    public static FlowDocument Build(string? markdown)
    {
        var document = new FlowDocument
        {
            Background = MediaBrushes.Transparent,
            Foreground = PrimaryText,
            FontFamily = new MediaFontFamily("Segoe UI"),
            FontSize = 15,
            LineHeight = 25,
            PagePadding = new Thickness(0)
        };

        if (string.IsNullOrWhiteSpace(markdown))
        {
            document.Blocks.Add(new Paragraph(new Run("任务完成后，正文会显示在这里。")) { Foreground = SecondaryText });
            return document;
        }

        var inCodeBlock = false;
        Paragraph? codeParagraph = null;
        foreach (var rawLine in markdown.Replace("\r\n", "\n", StringComparison.Ordinal).Split('\n'))
        {
            var line = rawLine.TrimEnd();
            if (line.StartsWith("```", StringComparison.Ordinal))
            {
                inCodeBlock = !inCodeBlock;
                if (inCodeBlock)
                {
                    codeParagraph = new Paragraph
                    {
                        FontFamily = new MediaFontFamily("Cascadia Mono"),
                        FontSize = 13,
                        Background = new SolidColorBrush(MediaColor.FromRgb(20, 28, 43)),
                        Padding = new Thickness(14),
                        Margin = new Thickness(0, 8, 0, 12)
                    };
                    document.Blocks.Add(codeParagraph);
                }

                continue;
            }

            if (inCodeBlock)
            {
                codeParagraph?.Inlines.Add(new Run(line + Environment.NewLine));
                continue;
            }

            if (string.IsNullOrWhiteSpace(line))
            {
                continue;
            }

            if (line.StartsWith("### ", StringComparison.Ordinal))
            {
                document.Blocks.Add(Heading(line[4..], 17, new Thickness(0, 16, 0, 4)));
            }
            else if (line.StartsWith("## ", StringComparison.Ordinal))
            {
                document.Blocks.Add(Heading(line[3..], 19, new Thickness(0, 20, 0, 6)));
            }
            else if (line.StartsWith("# ", StringComparison.Ordinal))
            {
                document.Blocks.Add(Heading(line[2..], 25, new Thickness(0, 0, 0, 14)));
            }
            else if (line.StartsWith("- ", StringComparison.Ordinal) || line.StartsWith("* ", StringComparison.Ordinal))
            {
                var paragraph = new Paragraph { Margin = new Thickness(12, 2, 0, 2) };
                paragraph.Inlines.Add(new Run("• ") { Foreground = Accent, FontWeight = FontWeights.Bold });
                paragraph.Inlines.Add(new Run(line[2..]));
                document.Blocks.Add(paragraph);
            }
            else if (line is "---" or "***")
            {
                document.Blocks.Add(new BlockUIContainer(new System.Windows.Controls.Border
                {
                    Height = 1,
                    Background = new SolidColorBrush(MediaColor.FromRgb(47, 59, 79)),
                    Margin = new Thickness(0, 12, 0, 12)
                }));
            }
            else
            {
                document.Blocks.Add(new Paragraph(new Run(line)) { Margin = new Thickness(0, 2, 0, 8) });
            }
        }

        return document;
    }

    private static Paragraph Heading(string text, double size, Thickness margin)
    {
        return new Paragraph(new Run(text))
        {
            FontSize = size,
            FontWeight = FontWeights.SemiBold,
            Foreground = PrimaryText,
            Margin = margin
        };
    }
}
