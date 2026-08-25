using System.Diagnostics;
using System.Windows;
using System.Windows.Documents;
using System.Windows.Media;
using Markdig;
using Markdig.Extensions.Tables;
using Markdig.Syntax;
using Markdig.Syntax.Inlines;
using WpfBlock = System.Windows.Documents.Block;
using WpfInline = System.Windows.Documents.Inline;
using WpfList = System.Windows.Documents.List;
using WpfTable = System.Windows.Documents.Table;
using MediaBrush = System.Windows.Media.Brush;
using MediaBrushes = System.Windows.Media.Brushes;
using MediaColor = System.Windows.Media.Color;
using MediaFontFamily = System.Windows.Media.FontFamily;

namespace AIDecisionCenter.App.Converters;

public static class MarkdownDocumentBuilder
{
    private static readonly MarkdownPipeline Pipeline = new MarkdownPipelineBuilder()
        .UseAdvancedExtensions()
        .Build();

    private static readonly MediaBrush PrimaryText = FrozenBrush(232, 238, 248);
    private static readonly MediaBrush SecondaryText = FrozenBrush(166, 178, 199);
    private static readonly MediaBrush Accent = FrozenBrush(70, 211, 166);
    private static readonly MediaBrush Border = FrozenBrush(47, 59, 79);
    private static readonly MediaBrush CodeBackground = FrozenBrush(20, 28, 43);
    private static readonly MediaBrush QuoteBackground = FrozenBrush(24, 39, 53);

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

        AddBlocks(document.Blocks, Markdown.Parse(markdown, Pipeline));
        return document;
    }

    public static string ToPlainText(string? markdown)
    {
        var document = Build(markdown);
        return new TextRange(document.ContentStart, document.ContentEnd).Text.Trim();
    }

    private static void AddBlocks(BlockCollection destination, ContainerBlock source)
    {
        foreach (var block in source)
        {
            var rendered = RenderBlock(block);
            if (rendered is not null)
            {
                destination.Add(rendered);
            }
        }
    }

    private static WpfBlock? RenderBlock(Markdig.Syntax.Block block) => block switch
    {
        HeadingBlock heading => RenderHeading(heading),
        ParagraphBlock paragraph => RenderParagraph(paragraph),
        ListBlock list => RenderList(list),
        QuoteBlock quote => RenderQuote(quote),
        FencedCodeBlock code => RenderCode(code),
        CodeBlock code => RenderCode(code),
        ThematicBreakBlock => RenderRule(),
        Markdig.Extensions.Tables.Table table => RenderTable(table),
        ContainerBlock container => RenderSection(container),
        LeafBlock leaf => RenderLeaf(leaf),
        _ => null
    };

    private static Paragraph RenderHeading(HeadingBlock heading)
    {
        var sizes = new[] { 27d, 23d, 20d, 18d, 16d, 15d };
        var paragraph = new Paragraph
        {
            FontSize = sizes[Math.Clamp(heading.Level, 1, 6) - 1],
            FontWeight = FontWeights.SemiBold,
            Foreground = PrimaryText,
            Margin = heading.Level == 1 ? new Thickness(0, 2, 0, 14) : new Thickness(0, 18, 0, 6),
            KeepWithNext = true
        };
        AddInlines(paragraph.Inlines, heading.Inline);
        return paragraph;
    }

    private static Paragraph RenderParagraph(ParagraphBlock source)
    {
        var paragraph = new Paragraph { Margin = new Thickness(0, 2, 0, 10) };
        AddInlines(paragraph.Inlines, source.Inline);
        return paragraph;
    }

    private static WpfList RenderList(ListBlock source)
    {
        var list = new WpfList
        {
            MarkerStyle = source.IsOrdered ? TextMarkerStyle.Decimal : TextMarkerStyle.Disc,
            MarkerOffset = 18,
            Margin = new Thickness(18, 2, 0, 10),
            Padding = new Thickness(0)
        };
        foreach (var sourceItem in source.OfType<ListItemBlock>())
        {
            var item = new System.Windows.Documents.ListItem { Margin = new Thickness(0, 1, 0, 3) };
            foreach (var itemBlock in sourceItem)
            {
                var rendered = RenderBlock(itemBlock);
                if (rendered is not null)
                {
                    item.Blocks.Add(rendered);
                }
            }
            list.ListItems.Add(item);
        }
        return list;
    }

    private static Section RenderQuote(QuoteBlock source)
    {
        var section = new Section
        {
            Background = QuoteBackground,
            BorderBrush = Accent,
            BorderThickness = new Thickness(3, 0, 0, 0),
            Padding = new Thickness(14, 9, 12, 9),
            Margin = new Thickness(0, 6, 0, 12),
            Foreground = SecondaryText
        };
        AddBlocks(section.Blocks, source);
        return section;
    }

    private static Paragraph RenderCode(LeafBlock source) => new(new Run(source.Lines.ToString()))
    {
        FontFamily = new MediaFontFamily("Cascadia Mono"),
        FontSize = 13,
        LineHeight = 21,
        Background = CodeBackground,
        Padding = new Thickness(14),
        Margin = new Thickness(0, 7, 0, 12)
    };

    private static BlockUIContainer RenderRule() => new(new System.Windows.Controls.Border
    {
        Height = 1,
        Background = Border,
        Margin = new Thickness(0, 12, 0, 12)
    });

    private static WpfTable RenderTable(Markdig.Extensions.Tables.Table source)
    {
        var table = new WpfTable
        {
            CellSpacing = 0,
            Margin = new Thickness(0, 8, 0, 14),
            BorderBrush = Border,
            BorderThickness = new Thickness(1)
        };
        var columnCount = source.OfType<Markdig.Extensions.Tables.TableRow>().Select(row => row.Count).DefaultIfEmpty(1).Max();
        for (var index = 0; index < columnCount; index++)
        {
            table.Columns.Add(new TableColumn());
        }

        var group = new TableRowGroup();
        table.RowGroups.Add(group);
        foreach (var sourceRow in source.OfType<Markdig.Extensions.Tables.TableRow>())
        {
            var row = new System.Windows.Documents.TableRow
            {
                Background = sourceRow.IsHeader ? QuoteBackground : MediaBrushes.Transparent,
                FontWeight = sourceRow.IsHeader ? FontWeights.SemiBold : FontWeights.Normal
            };
            group.Rows.Add(row);
            foreach (var sourceCell in sourceRow.OfType<Markdig.Extensions.Tables.TableCell>())
            {
                var cell = new System.Windows.Documents.TableCell
                {
                    BorderBrush = Border,
                    BorderThickness = new Thickness(0, 0, 1, 1),
                    Padding = new Thickness(9, 6, 9, 6)
                };
                foreach (var cellBlock in sourceCell)
                {
                    var rendered = RenderBlock(cellBlock);
                    if (rendered is not null)
                    {
                        rendered.Margin = new Thickness(0);
                        cell.Blocks.Add(rendered);
                    }
                }
                row.Cells.Add(cell);
            }
        }
        return table;
    }

    private static Section RenderSection(ContainerBlock source)
    {
        var section = new Section { Margin = new Thickness(0) };
        AddBlocks(section.Blocks, source);
        return section;
    }

    private static Paragraph RenderLeaf(LeafBlock source)
    {
        var paragraph = new Paragraph { Margin = new Thickness(0, 2, 0, 10) };
        AddInlines(paragraph.Inlines, source.Inline);
        return paragraph;
    }

    private static void AddInlines(InlineCollection destination, ContainerInline? source)
    {
        for (var inline = source?.FirstChild; inline is not null; inline = inline.NextSibling)
        {
            var rendered = RenderInline(inline);
            if (rendered is not null)
            {
                destination.Add(rendered);
            }
        }
    }

    private static WpfInline? RenderInline(Markdig.Syntax.Inlines.Inline inline) => inline switch
    {
        LiteralInline literal => new Run(literal.Content.ToString()),
        LineBreakInline => new LineBreak(),
        CodeInline code => new Run(code.Content)
        {
            FontFamily = new MediaFontFamily("Cascadia Mono"),
            FontSize = 13,
            Background = CodeBackground,
            Foreground = Accent
        },
        EmphasisInline emphasis => RenderEmphasis(emphasis),
        LinkInline link => RenderLink(link),
        HtmlEntityInline entity => new Run(entity.Transcoded.ToString()),
        ContainerInline container => RenderSpan(container),
        _ => null
    };

    private static Span RenderEmphasis(EmphasisInline source)
    {
        var span = RenderSpan(source);
        if (source.DelimiterChar is '*' or '_')
        {
            if (source.DelimiterCount >= 2)
            {
                span.FontWeight = FontWeights.Bold;
            }
            else
            {
                span.FontStyle = FontStyles.Italic;
            }
        }
        else if (source.DelimiterChar == '~')
        {
            span.TextDecorations = TextDecorations.Strikethrough;
        }
        return span;
    }

    private static WpfInline RenderLink(LinkInline source)
    {
        if (source.IsImage)
        {
            var imageText = RenderSpan(source);
            imageText.Foreground = SecondaryText;
            imageText.Inlines.InsertBefore(imageText.Inlines.FirstInline, new Run("[图片: "));
            imageText.Inlines.Add(new Run("]"));
            return imageText;
        }

        var hyperlink = new Hyperlink { Foreground = Accent, TextDecorations = TextDecorations.Underline };
        AddInlines(hyperlink.Inlines, source);
        if (Uri.TryCreate(source.Url, UriKind.Absolute, out var uri) && uri.Scheme is "http" or "https" or "mailto")
        {
            hyperlink.NavigateUri = uri;
            hyperlink.ToolTip = uri.ToString();
            hyperlink.RequestNavigate += (_, args) =>
            {
                Process.Start(new ProcessStartInfo(args.Uri.AbsoluteUri) { UseShellExecute = true });
                args.Handled = true;
            };
        }
        return hyperlink;
    }

    private static Span RenderSpan(ContainerInline source)
    {
        var span = new Span();
        AddInlines(span.Inlines, source);
        return span;
    }

    private static SolidColorBrush FrozenBrush(byte red, byte green, byte blue)
    {
        var brush = new SolidColorBrush(MediaColor.FromRgb(red, green, blue));
        brush.Freeze();
        return brush;
    }
}
