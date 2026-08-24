using AIDecisionCenter.Core.Parsing;

namespace AIDecisionCenter.Tests;

public sealed class PlainTextSanitizerTests
{
    [Fact]
    public void ConvertsHtmlFallbackToReadableText()
    {
        var result = PlainTextSanitizer.FromHtml("<h1>报告</h1><p>风险&amp;机会</p><ul><li>第一项</li></ul>");

        Assert.Contains("报告", result, StringComparison.Ordinal);
        Assert.Contains("风险&机会", result, StringComparison.Ordinal);
        Assert.Contains("第一项", result, StringComparison.Ordinal);
        Assert.DoesNotContain("<", result, StringComparison.Ordinal);
    }
}
