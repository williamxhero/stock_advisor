using AITradingCompanion.Core;

namespace AITradingCompanion.Tests;

public sealed class ProductPathsTests
{
    [Fact]
    public void ExplicitDirectoryWinsAndIsNormalized()
    {
        var root = Path.Combine(Path.GetTempPath(), "companion-explicit", "..");

        Assert.Equal(Path.GetFullPath(root), ProductPaths.ResolveDataRoot(root));
    }

    [Fact]
    public void DefaultDataRootIsUnderDApp()
    {
        Assert.Equal(@"D:\APP\AITradingCompanion", ProductPaths.DefaultDataRoot);
    }
}
