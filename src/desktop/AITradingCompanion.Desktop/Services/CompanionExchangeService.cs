using System.Security.Cryptography;
using System.Text;
using System.Text.Json;

namespace AITradingCompanion.Desktop.Services;

/// <summary>File-only boundary between the desktop client and the project-owned Python runtime.</summary>
public sealed class CompanionExchangeService
{
    private static readonly JsonSerializerOptions IndentedJson = new() { WriteIndented = true };
    private static readonly UTF8Encoding Utf8WithoutBom = new(encoderShouldEmitUTF8Identifier: false);
    private static readonly object SendGate = new();
    private readonly AppPaths _paths;

    public CompanionExchangeService(AppPaths paths) => _paths = paths;

    public Task SendAsync(object command, CancellationToken cancellationToken = default)
    {
        lock (SendGate)
        {
            cancellationToken.ThrowIfCancellationRequested();
            Directory.CreateDirectory(_paths.CompanionToRuntimePendingDirectory);
            var json = JsonSerializer.Serialize(command, IndentedJson);
            var payload = JsonSerializer.Deserialize<Dictionary<string, object?>>(json)!
                ?? throw new InvalidOperationException("command must serialize to an object");
            var commandId = ReadString(payload, "command_id")
                ?? throw new InvalidOperationException("command_id is required");
            AddCausalMetadata(payload);
            var unsignedBody = JsonSerializer.Serialize(payload, IndentedJson);
            payload["sha256"] = Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(unsignedBody))).ToLowerInvariant();
            var body = JsonSerializer.Serialize(payload, IndentedJson);
            var target = Path.Combine(_paths.CompanionToRuntimePendingDirectory, $"{commandId}.json");
            if (File.Exists(target))
            {
                var existing = File.ReadAllText(target, Encoding.UTF8);
                if (!string.Equals(existing, body, StringComparison.Ordinal))
                    throw new InvalidOperationException($"exchange command id conflict: {commandId}");
                return Task.CompletedTask;
            }

            var temporary = $"{target}.{Guid.NewGuid():N}.tmp";
            File.WriteAllText(temporary, body, Utf8WithoutBom);
            File.Move(temporary, target);
        }
        return Task.CompletedTask;
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
        long maximum = 0;
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
        return maximum + 1;
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
