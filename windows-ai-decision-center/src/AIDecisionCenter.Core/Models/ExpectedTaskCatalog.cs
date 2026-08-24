namespace AIDecisionCenter.Core.Models;

public static class ExpectedTaskCatalog
{
    public static IReadOnlyList<ExpectedTask> AShareTasks { get; } =
    [
        new(new TimeOnly(9, 0), "盘前机会发现"),
        new(new TimeOnly(9, 45), "开盘异常发现"),
        new(new TimeOnly(10, 30), "趋势确认"),
        new(new TimeOnly(14, 30), "操作决策"),
        new(new TimeOnly(15, 20), "收盘复盘")
    ];
}
