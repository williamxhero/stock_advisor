using AIDecisionCenter.App.ViewModels;
using AIDecisionCenter.Core.Models;

namespace AIDecisionCenter.Tests;

public sealed class TaskRowViewModelTests
{
    [Fact]
    public void MissingTaskBecomesPassedAtTwentyMinuteDeadline()
    {
        var row = new TaskRowViewModel(
            new ExpectedTask(new TimeOnly(9, 0), "盘前机会发现"),
            null,
            TimeSpan.FromMinutes(20));

        row.RefreshStatus(new DateTime(2026, 8, 24, 9, 19, 59));
        Assert.False(row.IsPassed);
        Assert.Equal("等待", row.StatusText);

        row.RefreshStatus(new DateTime(2026, 8, 24, 9, 20, 0));
        Assert.True(row.IsPassed);
        Assert.Equal("PASS", row.StatusText);
    }
}
