using System.Security.Cryptography;
using System.Text;
using System.Text.Json;

namespace AIDecisionCenter.App.Services;

/// <summary>File-only boundary between the desktop client and the project-owned Python runtime.</summary>
public sealed class CompanionExchangeService
{
    private static readonly JsonSerializerOptions IndentedJson = new() { WriteIndented = true };
    private static readonly UTF8Encoding Utf8WithoutBom = new(encoderShouldEmitUTF8Identifier: false);
    private readonly AppPaths _paths;

    public CompanionExchangeService(AppPaths paths) => _paths = paths;

    public async Task SendAsync(object command, CancellationToken cancellationToken = default)
    {
        Directory.CreateDirectory(_paths.CompanionToRuntimePendingDirectory);
        var json = JsonSerializer.Serialize(command, IndentedJson);
        var payload = JsonSerializer.Deserialize<Dictionary<string, object?>>(json)!;
        payload["sha256"] = Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(json))).ToLowerInvariant();
        var body = JsonSerializer.Serialize(payload, IndentedJson);
        var commandId = payload["command_id"]?.ToString() ?? throw new InvalidOperationException("command_id is required");
        var target = Path.Combine(_paths.CompanionToRuntimePendingDirectory, $"{commandId}.json");
        if (File.Exists(target))
        {
            return;
        }

        var temporary = $"{target}.{Guid.NewGuid():N}.tmp";
        await File.WriteAllTextAsync(temporary, body, Utf8WithoutBom, cancellationToken).ConfigureAwait(false);
        File.Move(temporary, target);
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
