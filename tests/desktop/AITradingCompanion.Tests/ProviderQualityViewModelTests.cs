using System.Text.Json.Nodes;
using AITradingCompanion.Desktop.ViewModels;

namespace AITradingCompanion.Tests;

public sealed class ProviderQualityViewModelTests
{
    [Fact]
    public void ParseQualityPreservesTechnicalRatesLatencyCostAndRaceCounters()
    {
        var payload = JsonNode.Parse("""
        {
          "items": [{
            "endpoint_id": "claude-relay", "model": "claude-opus-5", "model_family": "anthropic", "stage": "judgment",
            "sample_size": 17, "insufficient_data": true, "protocol_success_rate": 0.9, "product_success_rate": 0.8, "no_first_token_rate": 0.1,
            "ttft_ms": {"p50": 620.0, "p95": 1400.0}, "duration_ms": {"p50": 2200.0, "p95": 4100.0}, "currency": "USD", "estimated_cost_total": 3.2,
            "actual_cost_total": 3.0, "average_cost_per_product_success": 0.23,
            "average_actual_cost_per_product_success": 0.21, "win_count": 9, "delayed_start_count": 4,
            "suspicious_cancel_count": 2, "suspicious_cancel_estimated_cost": 0.3, "suspicious_cancel_actual_cost": 0.2
          }]
        }
        """)!.AsObject();

        var row = Assert.Single(ProviderQualityViewModel.ParseQuality(payload));

        Assert.Equal("claude-relay", row.Endpoint);
        Assert.Equal("anthropic", row.Family);
        Assert.Equal(17, row.Samples);
        Assert.True(row.Insufficient);
        Assert.Equal(620.0, row.TtftP50);
        Assert.Equal(2200.0, row.DurationP50);
        Assert.Equal("USD", row.Currency);
        Assert.Equal(0.8, row.ProductSuccess);
        Assert.Equal(0.1, row.NoFirstToken);
        Assert.Equal(3.0, row.ActualCost);
        Assert.Equal(4, row.Delayed);
        Assert.Equal(2, row.SuspiciousCancels);
        Assert.Equal(0.2, row.SuspiciousActualCost);
    }

    [Fact]
    public void ParseErrorsPreservesGroupingAndSampleSize()
    {
        var payload = JsonNode.Parse("""{"items":[{"endpoint_id":"gpt","model":"gpt-5","model_family":"openai","stage":"research","error":"rate_limited","count":3,"rate":0.15,"sample_size":20}]}""")!.AsObject();
        var row = Assert.Single(ProviderQualityViewModel.ParseErrors(payload));
        Assert.Equal("rate_limited", row.Error);
        Assert.Equal(3, row.Count);
        Assert.Equal(0.15, row.Rate);
        Assert.Equal(20, row.Samples);
    }
}
