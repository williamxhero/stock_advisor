namespace AITradingCompanion.Core.Models;

public static class ExpectedTaskCatalog
{
    public static IReadOnlyList<ExpectedTask> AShareTasks { get; } =
    [
        new("daily.opportunity.0900", new TimeOnly(9, 0), "盘前机会发现"),
        new("daily.execution.0945", new TimeOnly(9, 45), "开盘异常发现"),
        new("daily.execution.1030", new TimeOnly(10, 30), "趋势确认"),
        new("daily.execution.1430", new TimeOnly(14, 30), "操作决策"),
        new("daily.review.1520", new TimeOnly(15, 20), "收盘复盘")
    ];
}
