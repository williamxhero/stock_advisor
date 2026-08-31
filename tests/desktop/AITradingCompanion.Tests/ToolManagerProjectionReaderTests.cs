using AITradingCompanion.ToolManager;

namespace AITradingCompanion.Tests;

public sealed class ToolManagerProjectionReaderTests
{
    [Fact]
    public void ReadsOnlyTheVersionedExchangeProjection()
    {
        var root = Path.Combine(Path.GetTempPath(), Guid.NewGuid().ToString("N"));
        var projection = Path.Combine(root, "exchange", "tool-manager", "projection.json");
        Directory.CreateDirectory(Path.GetDirectoryName(projection)!);
        File.WriteAllText(projection, """{"contract":"ai-trading-tool-manager-projection/v1","updated_at":"2026-09-01T07:00:00Z","needs":[{"capability":"quote","state":"queued","urgency":"high","occurrence_count":2,"updated_at":"2026-09-01T07:00:00Z"}],"tools":[{"capability":"quote","version":"1.0.0","health":"healthy","degrade_reason":"","audit_reference":"artifact:sha256:test"}]}""");

        var result = new ToolManagerProjectionReader(root).Read();

        Assert.Equal("已连接 Exchange", result.Status);
        Assert.Equal("quote", Assert.Single(result.Needs).Capability);
        Assert.Equal("healthy", Assert.Single(result.Tools).Health);
    }
}
