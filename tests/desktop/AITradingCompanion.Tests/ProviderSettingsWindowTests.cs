using System.Reflection;
using System.Text.Json.Nodes;
using AITradingCompanion.Desktop.Views;

namespace AITradingCompanion.Tests;

public sealed class ProviderSettingsWindowTests
{
    [Fact]
    public void UnexpectedNumericLegacyFieldIsTreatedAsMissingTextInsteadOfCrashingTheWindow()
    {
        var text = typeof(ProviderSettingsWindow).GetMethod(
            "Text",
            BindingFlags.Static | BindingFlags.NonPublic);
        Assert.NotNull(text);

        var endpoint = JsonNode.Parse("""{"model":123}""")!.AsObject();
        var result = text!.Invoke(null, [endpoint, "model", ""]);

        Assert.Equal("", result);
    }

    [Fact]
    public void EndpointEditPreservesRealModelDirectoryStatus()
    {
        var preserve = typeof(ProviderSettingsWindow).GetMethod(
            "PreserveInventoryMetadata",
            BindingFlags.Static | BindingFlags.NonPublic);
        Assert.NotNull(preserve);
        var source = JsonNode.Parse("""{"available_models":["gpt-5.6-sol"],"model_directory_status":"available","models_updated_at":"2026-08-28T00:00:00Z"}""")!.AsObject();
        var target = new JsonObject();

        preserve!.Invoke(null, [source, target]);

        Assert.Equal("available", target["model_directory_status"]!.GetValue<string>());
        Assert.Equal("gpt-5.6-sol", target["available_models"]![0]!.GetValue<string>());
    }
}
