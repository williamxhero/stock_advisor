using AITradingCompanion.Desktop.ViewModels;

namespace AITradingCompanion.Tests;

public sealed class MainViewModelStatusTests
{
    [Fact]
    public void SuccessfulTradingDayRefreshClearsItsEarlierConnectionError()
    {
        var recovered = MainViewModel.TradingDayStatusAfterSuccess(
            "交易日状态暂不可用：由于目标计算机积极拒绝，无法连接。");

        Assert.Equal(string.Empty, recovered);
    }

    [Fact]
    public void SuccessfulTradingDayRefreshDoesNotClearANewerUnrelatedStatus()
    {
        var recovered = MainViewModel.TradingDayStatusAfterSuccess("正在扫描本地 Inbox…");

        Assert.Equal("正在扫描本地 Inbox…", recovered);
    }
}
