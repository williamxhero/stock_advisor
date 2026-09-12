using System.Collections.Concurrent;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace AITradingCompanion.Desktop.Services;

/// <summary>File-only boundary between the desktop client and the project-owned Python runtime.</summary>
public sealed class CompanionExchangeService
{
    private static readonly JsonSerializerOptions IndentedJson = new() { WriteIndented = true };
    private static readonly UTF8Encoding Utf8WithoutBom = new(encoderShouldEmitUTF8Identifier: false);
    private static readonly ConcurrentDictionary<string, SemaphoreSlim> CycleGates = new(StringComparer.Ordinal);
    private readonly AppPaths _paths;

    public CompanionExchangeService(AppPaths paths) => _paths = paths;

    public async Task SendAsync(object command, CancellationToken cancellationToken = default)
    {
        var json = JsonSerializer.Serialize(command, IndentedJson);
        var payload = JsonSerializer.Deserialize<Dictionary<string, object?>>(json)
            ?? throw new InvalidOperationException("command must serialize to an object");
        var stream = ReadString(payload, "cycle_id");
        var gateKey = stream is null
            ? null
            : $"{Path.GetFullPath(_paths.CompanionExchangeRoot)}|{stream}";
        var gate = gateKey is null ? null : CycleGates.GetOrAdd(gateKey, _ => new SemaphoreSlim(1, 1));
        if (gate is not null) await gate.WaitAsync(cancellationToken).ConfigureAwait(false);
        try
        {
            cancellationToken.ThrowIfCancellationRequested();
            WriteCommand(payload);
        }
        finally
        {
            gate?.Release();
        }
    }

    private void WriteCommand(Dictionary<string, object?> payload)
    {
        Directory.CreateDirectory(_paths.CompanionToRuntimePendingDirectory);
        var commandId = ReadString(payload, "command_id")
            ?? throw new InvalidOperationException("command_id is required");
        var existing = FindExistingCommand(commandId);
        if (existing is not null)
        {
            if (!EquivalentCommand(existing, payload))
                throw new InvalidOperationException($"exchange command id conflict: {commandId}");
            return;
        }

        AddCausalMetadata(payload);
        var unsignedBody = JsonSerializer.Serialize(payload, IndentedJson);
        payload["sha256"] = Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(unsignedBody))).ToLowerInvariant();
        var body = JsonSerializer.Serialize(payload, IndentedJson);
        var target = Path.Combine(_paths.CompanionToRuntimePendingDirectory, $"{commandId}.json");
        var temporary = $"{target}.{Guid.NewGuid():N}.tmp";
        File.WriteAllText(temporary, body, Utf8WithoutBom);
        File.Move(temporary, target);
    }

    private Dictionary<string, object?>? FindExistingCommand(string commandId)
    {
        foreach (var state in new[] { "pending", "processing", "processed", "dead-letter" })
        {
            var path = Path.Combine(_paths.CompanionExchangeRoot, "to-runtime", state, $"{commandId}.json");
            if (!File.Exists(path)) continue;
            try
            {
                using var document = JsonDocument.Parse(File.ReadAllText(path, Encoding.UTF8));
                var element = document.RootElement;
                if (element.TryGetProperty("received", out var received) && received.ValueKind == JsonValueKind.Object)
                    element = received;
                return JsonSerializer.Deserialize<Dictionary<string, object?>>(element.GetRawText());
            }
            catch (JsonException) { return null; }
            catch (IOException) { return null; }
        }
        return null;
    }

    private static bool EquivalentCommand(
        Dictionary<string, object?> existing,
        Dictionary<string, object?> requested)
    {
        static JsonObject Normalize(Dictionary<string, object?> value)
        {
            var node = JsonSerializer.SerializeToNode(value)?.AsObject()
                ?? throw new InvalidOperationException("command must serialize to an object");
            node.Remove("sha256");
            node.Remove("causal_stream");
            node.Remove("causal_sequence");
            return node;
        }
        return JsonNode.DeepEquals(Normalize(existing), Normalize(requested));
    }

    private void AddCausalMetadata(Dictionary<string, object?> payload)
    {
        if (!string.Equals(ReadString(payload, "contract"), "companion-user-command/v1", StringComparison.Ordinal)
            || payload.ContainsKey("causal_stream") || payload.ContainsKey("causal_sequence")) return;
        var stream = ReadString(payload, "cycle_id");
        if (string.IsNullOrWhiteSpace(stream)) return;
        payload["causal_stream"] = stream;
        payload["causal_sequence"] = NextCausalSequence(stream);
    }

    private long NextCausalSequence(string stream)
    {
        Directory.CreateDirectory(_paths.CompanionCausalSequenceDirectory);
        var statePath = Path.Combine(
            _paths.CompanionCausalSequenceDirectory,
            $"{Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(stream))).ToLowerInvariant()}.json");
        long next = 1;
        if (File.Exists(statePath))
        {
            try
            {
                using var state = JsonDocument.Parse(File.ReadAllText(statePath, Encoding.UTF8));
                if (state.RootElement.TryGetProperty("next_sequence", out var stored)
                    && stored.TryGetInt64(out var storedNext)) next = Math.Max(next, storedNext);
            }
            catch (JsonException) { }
            catch (IOException) { }
        }
        var maximum = 0L;
        foreach (var state in new[] { "pending", "processing", "processed", "dead-letter" })
        {
            var directory = Path.Combine(_paths.CompanionToRuntimePendingDirectory, "..", state);
            if (!Directory.Exists(directory)) continue;
            foreach (var path in Directory.EnumerateFiles(directory, "*.json"))
            {
                try
                {
                    using var document = JsonDocument.Parse(File.ReadAllText(path, Encoding.UTF8));
                    var element = document.RootElement;
                    if (element.TryGetProperty("received", out var received) && received.ValueKind == JsonValueKind.Object)
                        element = received;
                    if (element.TryGetProperty("causal_stream", out var causalStream)
                        && causalStream.GetString() == stream
                        && element.TryGetProperty("causal_sequence", out var sequence)
                        && sequence.TryGetInt64(out var number))
                        maximum = Math.Max(maximum, number);
                }
                catch (JsonException) { }
                catch (IOException) { }
            }
        }
        next = Math.Max(next, maximum + 1);
        var stateBody = JsonSerializer.Serialize(new
        {
            contract = "companion-causal-sequence/v1",
            causal_stream = stream,
            next_sequence = next + 1,
        }, IndentedJson);
        var temporary = $"{statePath}.{Guid.NewGuid():N}.tmp";
        File.WriteAllText(temporary, stateBody, Utf8WithoutBom);
        File.Move(temporary, statePath, overwrite: true);
        return next;
    }

    private static string? ReadString(Dictionary<string, object?> payload, string key)
    {
        if (!payload.TryGetValue(key, out var value) || value is null) return null;
        if (value is JsonElement element && element.ValueKind == JsonValueKind.String) return element.GetString();
        return value.ToString();
    }

    public IReadOnlyList<string> ReadLatestEvents(int maximum = 20)
    {
        Directory.CreateDirectory(_paths.CompanionToClientPendingDirectory);
        Directory.CreateDirectory(_paths.CompanionToClientProcessedDirectory);
        foreach (var pending in Directory.EnumerateFiles(_paths.CompanionToClientPendingDirectory, "*.json"))
        {
            var destination = Path.Combine(_paths.CompanionToClientProcessedDirectory, Path.GetFileName(pending));
            try { File.Move(pending, destination); }
            // Another refresh already claimed this id; leave the source for explicit inspection.
            catch (IOException) when (File.Exists(destination)) { }
        }
        if (!Directory.Exists(_paths.CompanionToClientProcessedDirectory)) return [];
        return Directory.EnumerateFiles(_paths.CompanionToClientProcessedDirectory, "*.json")
            .OrderByDescending(File.GetLastWriteTimeUtc).Take(maximum)
            .Select(path => File.ReadAllText(path, Encoding.UTF8)).ToArray();
    }
}
