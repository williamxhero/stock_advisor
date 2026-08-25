using System.Text.Json.Nodes;
using AITradingCompanion.Core.Models;
using AITradingCompanion.Core.Parsing;

namespace AITradingCompanion.Tests;

public sealed class DecisionMessageParserTests
{
    [Fact]
    public void ParsesVersionedLocalMessage()
    {
        var json = File.ReadAllText(Path.Combine(AppContext.BaseDirectory, "Fixtures", "ai-decision-message-v1.json"));

        var parsed = DecisionMessageParser.TryParse(json, DateTimeOffset.UtcNow, out var message, out var error);

        Assert.True(parsed, error);
        Assert.NotNull(message);
        Assert.Equal("daily.execution.1430", message.TaskKey);
        Assert.Equal(TaskMessageStatus.Succeeded, message.Status);
        Assert.Equal("操作决策", message.TaskType);
    }

    [Fact]
    public void RejectsContentWithWrongHash()
    {
        var node = JsonNode.Parse(TestMessageFactory.CreateEnvelope())!.AsObject();
        node["response_sha256"] = new string('0', 64);

        var parsed = DecisionMessageParser.TryParse(node.ToJsonString(), DateTimeOffset.UtcNow, out _, out var error);

        Assert.False(parsed);
        Assert.Contains("sha256", error, StringComparison.OrdinalIgnoreCase);
    }

    [Fact]
    public void RejectsUnknownPropertiesSoContractDriftIsVisible()
    {
        var node = JsonNode.Parse(TestMessageFactory.CreateEnvelope())!.AsObject();
        node["unexpected"] = true;

        Assert.False(DecisionMessageParser.TryParse(node.ToJsonString(), DateTimeOffset.UtcNow, out _, out _));
    }
}
