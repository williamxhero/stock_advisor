using AIDecisionCenter.App.Services;

namespace AIDecisionCenter.Tests;

public sealed class TaskPollingScheduleTests
{
    private static readonly TimeSpan RetryInterval = TimeSpan.FromSeconds(30);
    private static readonly TimeSpan NodeTimeout = TimeSpan.FromMinutes(20);

    [Fact]
    public void BeforeFirstNodeWaitsUntilFirstNode()
    {
        var delay = TaskPollingSchedule.GetDelay(
            new DateTime(2026, 8, 24, 8, 30, 0),
            new DateOnly(2026, 8, 24),
            [],
            null,
            RetryInterval,
            NodeTimeout);

        Assert.Equal(TimeSpan.FromMinutes(30), delay);
    }

    [Fact]
    public void AtDueNodeChecksImmediatelyWhenItHasNotCheckedSinceNodeTime()
    {
        var delay = TaskPollingSchedule.GetDelay(
            new DateTime(2026, 8, 24, 9, 0, 0),
            new DateOnly(2026, 8, 24),
            [],
            new DateTime(2026, 8, 24, 8, 59, 55),
            RetryInterval,
            NodeTimeout);

        Assert.Equal(TimeSpan.Zero, delay);
    }

    [Fact]
    public void MissingDueNodeRetriesThirtySecondsAfterLastCheck()
    {
        var delay = TaskPollingSchedule.GetDelay(
            new DateTime(2026, 8, 24, 9, 0, 10),
            new DateOnly(2026, 8, 24),
            [],
            new DateTime(2026, 8, 24, 9, 0, 0),
            RetryInterval,
            NodeTimeout);

        Assert.Equal(TimeSpan.FromSeconds(20), delay);
    }

    [Fact]
    public void CompletedCurrentNodeWaitsUntilNextIncompleteNode()
    {
        var delay = TaskPollingSchedule.GetDelay(
            new DateTime(2026, 8, 24, 9, 0, 5),
            new DateOnly(2026, 8, 24),
            [new TimeOnly(9, 0)],
            new DateTime(2026, 8, 24, 9, 0, 4),
            RetryInterval,
            NodeTimeout);

        Assert.Equal(TimeSpan.FromMinutes(44) + TimeSpan.FromSeconds(55), delay);
    }

    [Fact]
    public void AllNodesCompletedWaitsUntilTomorrowFirstNode()
    {
        var delay = TaskPollingSchedule.GetDelay(
            new DateTime(2026, 8, 24, 15, 20, 5),
            new DateOnly(2026, 8, 24),
            [new TimeOnly(9, 0), new TimeOnly(9, 45), new TimeOnly(10, 30), new TimeOnly(14, 30), new TimeOnly(15, 20)],
            new DateTime(2026, 8, 24, 15, 20, 4),
            RetryInterval,
            NodeTimeout);

        Assert.Equal(TimeSpan.FromHours(17) + TimeSpan.FromMinutes(39) + TimeSpan.FromSeconds(55), delay);
    }

    [Fact]
    public void CompletedSlotsFromYesterdayDoNotSkipTodayFirstNode()
    {
        var delay = TaskPollingSchedule.GetDelay(
            new DateTime(2026, 8, 25, 0, 0, 0),
            new DateOnly(2026, 8, 24),
            [new TimeOnly(9, 0), new TimeOnly(9, 45), new TimeOnly(10, 30), new TimeOnly(14, 30), new TimeOnly(15, 20)],
            new DateTime(2026, 8, 24, 15, 20, 0),
            RetryInterval,
            NodeTimeout);

        Assert.Equal(TimeSpan.FromHours(9), delay);
    }

    [Fact]
    public void RetryDelayStopsAtTwentyMinuteNodeDeadline()
    {
        var delay = TaskPollingSchedule.GetDelay(
            new DateTime(2026, 8, 24, 9, 19, 50),
            new DateOnly(2026, 8, 24),
            [],
            new DateTime(2026, 8, 24, 9, 19, 45),
            RetryInterval,
            NodeTimeout);

        Assert.Equal(TimeSpan.FromSeconds(10), delay);
    }

    [Fact]
    public void TimedOutNodeWaitsUntilNextNode()
    {
        var delay = TaskPollingSchedule.GetDelay(
            new DateTime(2026, 8, 24, 9, 20, 0),
            new DateOnly(2026, 8, 24),
            [],
            new DateTime(2026, 8, 24, 9, 19, 30),
            RetryInterval,
            NodeTimeout);

        Assert.Equal(TimeSpan.FromMinutes(25), delay);
    }
}
