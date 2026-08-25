using AIDecisionCenter.App.Services;

namespace AIDecisionCenter.Tests;

public sealed class PortfolioEventProjectionTests
{
    [Fact]
    public void ProjectsSnapshotAndCorrelatesProcessingStatusWithoutCycleEvents()
    {
        var events = new[]
        {
            """{"contract":"companion-client-event/v1","event_id":"cycle","cycle_id":"c1","type":"brief.ready","created_at":"2026-08-25T01:00:00Z","payload":{"brief":"M0","cycle":{"task_key":"daily.execution.0945"}}}""",
            """{"contract":"portfolio-client-event/v1","event_id":"started","type":"portfolio.interpretation.started","created_at":"2026-08-25T01:01:00Z","payload":{"source_artifact_id":"a1"}}""",
            """{"contract":"portfolio-client-event/v1","event_id":"applied","type":"portfolio.change.applied","created_at":"2026-08-25T01:02:00Z","payload":{"source_artifact_id":"a1","summary":"新泉股份 +100股 @ 38.23","snapshot":{"total_assets":230000,"updated_at":"2026-08-25T01:02:00Z","positions":[{"code":"603179","name":"新泉股份","shares":200,"average_cost":38.23,"last_price":38.23,"price_as_of":"2026-08-25T01:02:00Z","market_value":7646,"unrealized_pnl":0,"weight":0.0332,"updated_at":"2026-08-25T01:02:00Z","insight":{"theme_id":"THM-A","stock_role":"follower"}}],"transactions":[]}}}""",
        };

        var projection = PortfolioEventProjection.Project(events);

        Assert.NotNull(projection);
        Assert.Single(projection.Positions);
        Assert.Equal(200, projection.Positions[0].Shares);
        Assert.Equal("持仓已更新：新泉股份 +100股 @ 38.23", projection.StatusByArtifactId["a1"]);
    }
}
