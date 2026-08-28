using System.Text.Json.Nodes;

namespace AITradingCompanion.Desktop.Services;

public interface ICompanionGateway : IDisposable
{
    Task<JsonObject> GetSnapshotAsync(string kind, IReadOnlyDictionary<string, string>? query = null, CancellationToken cancellationToken = default);
    Task<JsonObject> SubmitCommandAsync(JsonObject command, CancellationToken cancellationToken = default);
    Task<JsonObject> GetHealthAsync(CancellationToken cancellationToken = default);
    Task<JsonObject> GetProviderQualityAsync(string view = "summary", IReadOnlyDictionary<string, string>? query = null, CancellationToken cancellationToken = default);
    Task<string> ExportProviderQualityAsync(string format, IReadOnlyDictionary<string, string>? query = null, CancellationToken cancellationToken = default);
    IAsyncEnumerable<JsonObject> SubscribeAsync(long afterSequence, CancellationToken cancellationToken = default);
}
