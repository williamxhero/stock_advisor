using AIDecisionCenter.App.Services;

namespace AIDecisionCenter.Tests;

public sealed class SpeechTextPreprocessorTests
{
    [Fact]
    public void RemovesLeadingReportTitleAndMetadataBlock()
    {
        const string text = """
            A股 14:30 操作决策（人工补跑）
            计划节点：2026-08-24 14:30（Asia/Shanghai）
            实际执行：2026-08-24 14:51–15:00
            Protocol：DailyExecution-v1.5
            Run ID：99c5d434-8a27-4a7d-8b17-ed49abd4dbb5
            结论先行
            今天不新开仓。
            """;

        var prepared = SpeechTextPreprocessor.Prepare(text);

        Assert.StartsWith("结论先行", prepared, StringComparison.Ordinal);
        Assert.DoesNotContain("人工补跑", prepared, StringComparison.Ordinal);
        Assert.DoesNotContain("计划节点", prepared, StringComparison.Ordinal);
        Assert.DoesNotContain("Run ID", prepared, StringComparison.Ordinal);
    }

    [Fact]
    public void RemovesMetadataBlockWithoutTitle()
    {
        const string text = "计划节点：09:00\nProtocol: Test-v1\n正文开始";

        Assert.Equal("正文开始", SpeechTextPreprocessor.Prepare(text));
    }

    [Fact]
    public void KeepsOrdinaryTextAndSingleLabelUnchanged()
    {
        const string text = "结论先行\nProtocol 是执行规则的一部分。\n继续持有。";

        Assert.Equal(text, SpeechTextPreprocessor.Prepare(text));
    }
}
