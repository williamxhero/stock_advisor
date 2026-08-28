using System.Collections.ObjectModel;
using System.ComponentModel;
using System.Runtime.CompilerServices;
using System.Text.Json.Nodes;
using AITradingCompanion.Desktop.Services;

namespace AITradingCompanion.Desktop.ViewModels;

public sealed record ProviderQualityRow(
    string Endpoint, string Model, string Family, string Stage, int Samples, bool Insufficient,
    double? ProtocolSuccess, double? ProductSuccess, double? NoFirstToken, double? TtftP50, double? TtftP95,
    double? DurationP50, double? DurationP95, string Currency,
    double? EstimatedCost, double? ActualCost, double? AverageEffectiveCost, double? AverageActualEffectiveCost,
    int Wins, int Delayed, int SuspiciousCancels, double? SuspiciousEstimatedCost, double? SuspiciousActualCost);

public sealed record ProviderErrorRow(string Endpoint, string Model, string Family, string Stage, string Error, int Count, double? Rate, int Samples);

public sealed class ProviderQualityViewModel : INotifyPropertyChanged, IDisposable
{
    private readonly ICompanionGateway _gateway;
    private string _window = "24h";
    private string _family = "all";
    private string _stage = "all";
    private string _sort = "product_success_rate";
    private string _status = "尚未加载";
    private bool _loading;

    public ProviderQualityViewModel(ICompanionGateway gateway) => _gateway = gateway;
    public ObservableCollection<ProviderQualityRow> Rows { get; } = [];
    public ObservableCollection<ProviderErrorRow> Errors { get; } = [];
    public IReadOnlyList<string> Windows { get; } = ["24h", "7d", "30d", "all"];
    public IReadOnlyList<string> Families { get; } = ["all", "openai", "anthropic"];
    public IReadOnlyList<string> Stages { get; } = ["all", "research", "judgment", "fast", "chat"];
    public IReadOnlyList<string> Sorts { get; } = ["product_success_rate", "protocol_success_rate", "ttft_p50", "duration_p50", "estimated_cost_total", "actual_cost_total", "sample_size"];
    public string Window { get => _window; set => Set(ref _window, value); }
    public string Family { get => _family; set => Set(ref _family, value); }
    public string Stage { get => _stage; set => Set(ref _stage, value); }
    public string Sort { get => _sort; set => Set(ref _sort, value); }
    public string Status { get => _status; private set => Set(ref _status, value); }
    public bool Loading { get => _loading; private set => Set(ref _loading, value); }

    public async Task RefreshAsync(CancellationToken cancellationToken = default)
    {
        Loading = true;
        try
        {
            var filters = Filters();
            var qualityTask = _gateway.GetProviderQualityAsync("comparison", filters, cancellationToken);
            var errorsTask = _gateway.GetProviderQualityAsync("errors", filters, cancellationToken);
            await Task.WhenAll(qualityTask, errorsTask).ConfigureAwait(true);
            Replace(Rows, ParseQuality(qualityTask.Result));
            Replace(Errors, ParseErrors(errorsTask.Result));
            Status = $"{Window} · {Rows.Count} 个 Provider/模型/阶段组合；“数据不足”不会作为停用依据";
        }
        catch (Exception exception) { Status = $"质量数据加载失败：{exception.Message}"; }
        finally { Loading = false; }
    }

    public Task<string> ExportAsync(string format, CancellationToken cancellationToken = default) =>
        _gateway.ExportProviderQualityAsync(format, Filters(), cancellationToken);

    public static IReadOnlyList<ProviderQualityRow> ParseQuality(JsonObject payload) =>
        (payload["items"] as JsonArray ?? []).OfType<JsonObject>().Select(item => new ProviderQualityRow(
            Text(item, "endpoint_id"), Text(item, "model"), Text(item, "model_family"), Text(item, "stage"),
            Integer(item, "sample_size"), Boolean(item, "insufficient_data"), Number(item, "protocol_success_rate"),
            Number(item, "product_success_rate"), Number(item, "no_first_token_rate"), NestedNumber(item, "ttft_ms", "p50"),
            NestedNumber(item, "ttft_ms", "p95"), NestedNumber(item, "duration_ms", "p50"),
            NestedNumber(item, "duration_ms", "p95"), Text(item, "currency"),
            Number(item, "estimated_cost_total"), Number(item, "actual_cost_total"),
            Number(item, "average_cost_per_product_success"), Number(item, "average_actual_cost_per_product_success"),
            Integer(item, "win_count"), Integer(item, "delayed_start_count"), Integer(item, "suspicious_cancel_count"),
            Number(item, "suspicious_cancel_estimated_cost"), Number(item, "suspicious_cancel_actual_cost"))).ToList();

    public static IReadOnlyList<ProviderErrorRow> ParseErrors(JsonObject payload) =>
        (payload["items"] as JsonArray ?? []).OfType<JsonObject>().Select(item => new ProviderErrorRow(
            Text(item, "endpoint_id"), Text(item, "model"), Text(item, "model_family"), Text(item, "stage"),
            Text(item, "error"), Integer(item, "count"), Number(item, "rate"), Integer(item, "sample_size"))).ToList();

    private Dictionary<string, string> Filters()
    {
        var result = new Dictionary<string, string> { ["window"] = Window, ["sort"] = Sort };
        if (Family != "all") result["family"] = Family;
        if (Stage != "all") result["stage"] = Stage;
        return result;
    }
    private static string Text(JsonObject item, string key) => item[key]?.GetValue<string>() ?? string.Empty;
    private static int Integer(JsonObject item, string key) => item[key]?.GetValue<int>() ?? 0;
    private static bool Boolean(JsonObject item, string key) => item[key]?.GetValue<bool>() ?? false;
    private static double? Number(JsonObject item, string key) => item[key] is null ? null : item[key]!.GetValue<double>();
    private static double? NestedNumber(JsonObject item, string parent, string key) => item[parent]?[key] is null ? null : item[parent]![key]!.GetValue<double>();
    private static void Replace<T>(ObservableCollection<T> target, IEnumerable<T> values) { target.Clear(); foreach (var value in values) target.Add(value); }
    private void Set<T>(ref T field, T value, [CallerMemberName] string? name = null) { if (EqualityComparer<T>.Default.Equals(field, value)) return; field = value; PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(name)); }
    public event PropertyChangedEventHandler? PropertyChanged;
    public void Dispose() => _gateway.Dispose();
}
