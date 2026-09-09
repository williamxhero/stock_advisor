namespace AITradingCompanion.Core;

public static class ProductPaths
{
    public const string DefaultDataRoot = @"D:\APP\AITradingCompanion";

    public static string ResolveDataRoot(string? explicitDirectory = null)
    {
        var configured = explicitDirectory
            ?? Environment.GetEnvironmentVariable("AI_TRADING_COMPANION_HOME")
            ?? DefaultDataRoot;
        return Path.GetFullPath(configured);
    }
}
