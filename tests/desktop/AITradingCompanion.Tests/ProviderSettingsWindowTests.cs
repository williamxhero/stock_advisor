using System.Reflection;
using System.Text.Json.Nodes;
using AITradingCompanion.Desktop.Views;

namespace AITradingCompanion.Tests;

public sealed class ProviderSettingsWindowTests
{
    [Fact]
    public void ProviderManagementRequiresAnExplicitKindAndDescribesM1AsOneRace()
    {
        var root = new DirectoryInfo(AppContext.BaseDirectory);
        while (root is not null && !File.Exists(Path.Combine(root.FullName, "AITradingCompanion.sln")))
            root = root.Parent;
        Assert.NotNull(root);
        var xaml = File.ReadAllText(Path.Combine(
            root.FullName, "src", "desktop", "AITradingCompanion.Desktop", "Views", "ProviderSettingsWindow.xaml"));

        Assert.Contains("x:Name=\"ProviderKind\"", xaml, StringComparison.Ordinal);
        Assert.Contains("Tag=\"single_family\"", xaml, StringComparison.Ordinal);
        Assert.Contains("Tag=\"cpa\"", xaml, StringComparison.Ordinal);
        Assert.Contains("M1 使用一次独立 race", xaml, StringComparison.Ordinal);
        Assert.DoesNotContain("GPT + Claude 盲判", xaml, StringComparison.Ordinal);
    }

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
