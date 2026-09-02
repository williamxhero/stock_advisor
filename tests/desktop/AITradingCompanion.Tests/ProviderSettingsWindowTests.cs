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
}
