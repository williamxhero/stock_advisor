namespace AITradingCompanion.Desktop.Services;

/// <summary>Allows an expensive timeline rebuild only when its visible content changes.</summary>
public sealed class TimelineRenderGate
{
    private string? _lastRenderKey;

    public bool ShouldRender(IEnumerable<string> entries)
    {
        var renderKey = string.Join('\u001e', entries);
        if (string.Equals(renderKey, _lastRenderKey, StringComparison.Ordinal)) return false;
        _lastRenderKey = renderKey;
        return true;
    }
}
