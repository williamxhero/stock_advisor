using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Net.Http;

namespace AITradingCompanion.Desktop.Services;

public sealed class LoopbackHttpGateway : ICompanionGateway
{
    private readonly AppPaths _paths;
    private readonly HttpClient _http = new() { Timeout = TimeSpan.FromSeconds(15) };
    public LoopbackHttpGateway(AppPaths paths) => _paths = paths;

    public Task<JsonObject> GetHealthAsync(CancellationToken cancellationToken = default) => GetAsync("/v1/health", cancellationToken);
    public Task<JsonObject> GetSnapshotAsync(string kind, IReadOnlyDictionary<string, string>? query = null, CancellationToken cancellationToken = default)
    {
        return GetAsync($"/v1/snapshots/{Uri.EscapeDataString(kind)}{Query(query)}", cancellationToken);
    }
    public async Task<JsonObject> SubmitCommandAsync(JsonObject command, CancellationToken cancellationToken = default)
    {
        var request = new HttpRequestMessage(HttpMethod.Post, Endpoint("/v1/commands")) { Content = new StringContent(command.ToJsonString(), Encoding.UTF8, "application/json") };
        Authorize(request);
        using var response = await _http.SendAsync(request, cancellationToken).ConfigureAwait(false);
        var body = await response.Content.ReadAsStringAsync(cancellationToken).ConfigureAwait(false);
        if (!response.IsSuccessStatusCode) throw new InvalidOperationException($"运行时拒绝命令：{body}");
        return JsonNode.Parse(body)?.AsObject() ?? throw new InvalidDataException("运行时返回无效 JSON。");
    }
    public async IAsyncEnumerable<JsonObject> SubscribeAsync(long afterSequence, [System.Runtime.CompilerServices.EnumeratorCancellation] CancellationToken cancellationToken = default)
    {
        var request = new HttpRequestMessage(HttpMethod.Get, Endpoint($"/v1/events?after={afterSequence}")); Authorize(request);
        using var response = await _http.SendAsync(request, HttpCompletionOption.ResponseHeadersRead, cancellationToken).ConfigureAwait(false);
        response.EnsureSuccessStatusCode();
        await using var stream = await response.Content.ReadAsStreamAsync(cancellationToken).ConfigureAwait(false);
        using var reader = new StreamReader(stream, Encoding.UTF8);
        while (!reader.EndOfStream && !cancellationToken.IsCancellationRequested)
        {
            var line = await reader.ReadLineAsync(cancellationToken).ConfigureAwait(false);
            if (line is null || !line.StartsWith("data: ", StringComparison.Ordinal)) continue;
            if (JsonNode.Parse(line[6..]) is JsonObject item) yield return item;
        }
    }
    private async Task<JsonObject> GetAsync(string path, CancellationToken cancellationToken)
    {
        var request = new HttpRequestMessage(HttpMethod.Get, Endpoint(path)); Authorize(request);
        using var response = await _http.SendAsync(request, cancellationToken).ConfigureAwait(false);
        var body = await response.Content.ReadAsStringAsync(cancellationToken).ConfigureAwait(false);
        if (!response.IsSuccessStatusCode) throw new InvalidOperationException($"运行时不可用：{body}");
        return JsonNode.Parse(body)?.AsObject() ?? throw new InvalidDataException("运行时返回无效 JSON。");
    }
    private async Task<string> GetTextAsync(string path, CancellationToken cancellationToken)
    {
        var request = new HttpRequestMessage(HttpMethod.Get, Endpoint(path)); Authorize(request);
        using var response = await _http.SendAsync(request, cancellationToken).ConfigureAwait(false);
        var body = await response.Content.ReadAsStringAsync(cancellationToken).ConfigureAwait(false);
        if (!response.IsSuccessStatusCode) throw new InvalidOperationException($"运行时不可用：{body}");
        return body;
    }
    private static string Query(IReadOnlyDictionary<string, string>? query) =>
        query is null || query.Count == 0 ? string.Empty : "?" + string.Join("&", query.Select(pair => $"{Uri.EscapeDataString(pair.Key)}={Uri.EscapeDataString(pair.Value)}"));
    private Uri Endpoint(string path)
    {
        var descriptor = Descriptor();
        if (!string.Equals(descriptor["contract"]?.GetValue<string>(), "companion-gateway/v1", StringComparison.Ordinal)) throw new InvalidOperationException("桌面端与运行时版本不兼容。");
        return new Uri($"http://127.0.0.1:{descriptor["port"]!.GetValue<int>()}{path}");
    }
    private JsonObject Descriptor()
    {
        var descriptorPath = Path.Combine(_paths.DataDirectory, "runtime", "gateway.json");
        return JsonNode.Parse(File.ReadAllText(descriptorPath))?.AsObject() ?? throw new InvalidOperationException("运行时 Gateway 尚未启动。");
    }
    private void Authorize(HttpRequestMessage request)
    {
        var token = Descriptor()["token"]?.GetValue<string>();
        if (string.IsNullOrWhiteSpace(token)) throw new InvalidOperationException("运行时 Gateway 缺少认证令牌。");
        request.Headers.Authorization = new System.Net.Http.Headers.AuthenticationHeaderValue("Bearer", token);
    }
    public void Dispose() => _http.Dispose();
}
