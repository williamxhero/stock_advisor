using AIDecisionCenter.Core.Models;

namespace AIDecisionCenter.Tests;

public sealed class ExpectedTaskCatalogTests
{
    [Fact]
    public void AShareTasksUseTheProductionSchedule()
    {
        var tasks = ExpectedTaskCatalog.AShareTasks;

        Assert.Collection(
            tasks,
            task => Assert.Equal((new TimeOnly(9, 0), "盘前机会发现"), (task.Slot, task.Name)),
            task => Assert.Equal((new TimeOnly(9, 45), "开盘异常发现"), (task.Slot, task.Name)),
            task => Assert.Equal((new TimeOnly(10, 30), "趋势确认"), (task.Slot, task.Name)),
            task => Assert.Equal((new TimeOnly(14, 30), "操作决策"), (task.Slot, task.Name)),
            task => Assert.Equal((new TimeOnly(15, 20), "收盘复盘"), (task.Slot, task.Name)));
    }
}
