using System.Windows;
using System.Windows.Documents;
using AIDecisionCenter.App.Converters;

namespace AIDecisionCenter.Tests;

public sealed class MarkdownDocumentBuilderTests
{
    [Fact]
    public void RendersCommonMarkdownAsFlowDocumentElements()
    {
        const string markdown = """
            # 决策标题

            正文包含 **粗体**、*斜体* 和 `代码`。

            1. 第一项
            2. 第二项

            > 风险提示

            | 股票 | 动作 |
            | --- | --- |
            | 示例 | 观察 |

            ```text
            raw code
            ```
            """;

        var document = MarkdownDocumentBuilder.Build(markdown);
        var blocks = document.Blocks.ToList();
        var text = new TextRange(document.ContentStart, document.ContentEnd).Text;
        var body = blocks.OfType<Paragraph>().Single(paragraph =>
            new TextRange(paragraph.ContentStart, paragraph.ContentEnd).Text.Contains("正文包含", StringComparison.Ordinal));

        Assert.Contains(blocks, block => block is Paragraph paragraph && paragraph.FontSize == 27);
        Assert.Contains(blocks, block => block is System.Windows.Documents.List list && list.MarkerStyle == TextMarkerStyle.Decimal);
        Assert.Contains(blocks, block => block is Section);
        Assert.Contains(blocks, block => block is Table);
        Assert.Contains(blocks, block => block is Paragraph paragraph && paragraph.FontFamily.Source == "Cascadia Mono");
        Assert.Contains(body.Inlines.OfType<Span>(), inline => inline.FontWeight == FontWeights.Bold);
        Assert.Contains(body.Inlines.OfType<Span>(), inline => inline.FontStyle == FontStyles.Italic);
        Assert.Contains(body.Inlines.OfType<Run>(), inline => inline.FontFamily.Source == "Cascadia Mono");
        Assert.Contains("决策标题", text, StringComparison.Ordinal);
        Assert.Contains("粗体", text, StringComparison.Ordinal);
        Assert.DoesNotContain("**粗体**", text, StringComparison.Ordinal);
    }

    [Fact]
    public void EmptyMarkdownShowsPlaceholder()
    {
        var document = MarkdownDocumentBuilder.Build(null);
        var text = new TextRange(document.ContentStart, document.ContentEnd).Text;

        Assert.Contains("正文会显示在这里", text, StringComparison.Ordinal);
    }

    [Fact]
    public void PlainTextForSpeechRemovesMarkdownMarkers()
    {
        var text = MarkdownDocumentBuilder.ToPlainText("# 标题\n\n这是 **重要结论** 和 `代码`。");

        Assert.Contains("标题", text, StringComparison.Ordinal);
        Assert.Contains("重要结论", text, StringComparison.Ordinal);
        Assert.DoesNotContain("#", text, StringComparison.Ordinal);
        Assert.DoesNotContain("**", text, StringComparison.Ordinal);
        Assert.DoesNotContain("`", text, StringComparison.Ordinal);
    }
}
