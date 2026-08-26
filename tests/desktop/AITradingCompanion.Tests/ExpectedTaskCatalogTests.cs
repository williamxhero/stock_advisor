using AITradingCompanion.Core.Models;

namespace AITradingCompanion.Tests;

public sealed class ExpectedTaskCatalogTests
{
    [Fact]
    public void DailyBoardUsesTaskKeysFromScheduleRegistry()
    {
        Assert.Collection(
            ExpectedTaskCatalog.AShareTasks,
            task => Assert.Equal(("conversation.daily", new TimeOnly(0, 0)), (task.TaskKey, task.Slot)),
            task => Assert.Equal(("daily.opportunity.0900", new TimeOnly(9, 0)), (task.TaskKey, task.Slot)),
            task => Assert.Equal(("daily.execution.0945", new TimeOnly(9, 45)), (task.TaskKey, task.Slot)),
            task => Assert.Equal(("daily.execution.1030", new TimeOnly(10, 30)), (task.TaskKey, task.Slot)),
            task => Assert.Equal(("daily.execution.1430", new TimeOnly(14, 30)), (task.TaskKey, task.Slot)),
            task => Assert.Equal(("daily.review.1520", new TimeOnly(15, 20)), (task.TaskKey, task.Slot)));
    }
}
