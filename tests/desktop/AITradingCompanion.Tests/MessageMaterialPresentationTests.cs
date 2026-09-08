using AITradingCompanion.Desktop.Services;

namespace AITradingCompanion.Tests;

public sealed class MessageMaterialPresentationTests
{
    [Fact]
    public void LinkOnlyEvidenceIsCompactedWithoutInternalProviderNames()
    {
        CompanionMessagePart[] parts =
        [
            new("material", "[查看tencent_minute](https://web.ifzq.gtimg.cn/appstock/app/minute/query?code=sh000001)", "tencent_minute", "https://web.ifzq.gtimg.cn/appstock/app/minute/query?code=sh000001", "index-sh"),
            new("material", "[查看tencent_minute](https://web.ifzq.gtimg.cn/appstock/app/minute/query?code=sz399001)", "tencent_minute", "https://web.ifzq.gtimg.cn/appstock/app/minute/query?code=sz399001", "index-sz"),
            new("material", "[查看eastmoney_breadth](https://push2delay.eastmoney.com/api/qt/clist/get)", "eastmoney_breadth", "https://push2delay.eastmoney.com/api/qt/clist/get", "breadth"),
            new("material", "[查看yosef_bounded_market_event_snapshot](https://www.cls.cn/detail/2475972)", "yosef_bounded_market_event_snapshot", "https://www.cls.cn/detail/2475972", "news"),
        ];

        var compact = MessageMaterialPresentation.CompactLinkOnlySources(parts);

        Assert.Equal(4, compact.Count);
        Assert.DoesNotContain("tencent_minute", compact.Markdown, StringComparison.Ordinal);
        Assert.DoesNotContain("eastmoney_breadth", compact.Markdown, StringComparison.Ordinal);
        Assert.DoesNotContain("yosef_bounded_market_event_snapshot", compact.Markdown, StringComparison.Ordinal);
        Assert.Contains("腾讯行情数据·上证指数", compact.Markdown, StringComparison.Ordinal);
        Assert.Contains("腾讯行情数据·深证成指", compact.Markdown, StringComparison.Ordinal);
        Assert.Contains("东方财富市场数据", compact.Markdown, StringComparison.Ordinal);
        Assert.Contains("财联社报道", compact.Markdown, StringComparison.Ordinal);
        Assert.DoesNotContain('\n', compact.Markdown);
    }

    [Fact]
    public void RichAttributedMaterialStaysInline()
    {
        var part = new CompanionMessagePart(
            "material",
            "> 公告原文：事项仍在推进。\n> [来源](https://example.com/notice)",
            "公告原文",
            "https://example.com/notice",
            "notice");

        Assert.False(MessageMaterialPresentation.IsLinkOnly(part));
    }
}
