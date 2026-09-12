using System.Text;
using System.Text.Json;
using AITradingCompanion.Desktop.Services;

namespace AITradingCompanion.Tests;

public sealed class CompanionExchangeServiceTests : IDisposable
{
    private readonly string _directory = Path.Combine(Path.GetTempPath(), "AITradingCompanion.Tests", Guid.NewGuid().ToString("N"));

    [Fact]
    public async Task SendWritesUtf8WithoutByteOrderMark()
    {
        var paths = new AppPaths(_directory);
        var exchange = new CompanionExchangeService(paths);
        var commandId = Guid.NewGuid().ToString();

        await exchange.SendAsync(new
        {
            contract = "companion-user-command/v1",
            command_id = commandId,
            cycle_id = "cycle-1",
            type = "begin_voice_capture",
        });

        var path = Path.Combine(paths.CompanionToRuntimePendingDirectory, $"{commandId}.json");
        var bytes = await File.ReadAllBytesAsync(path);
        Assert.False(bytes.AsSpan().StartsWith(Encoding.UTF8.Preamble));
    }

    [Fact]
    public async Task ReusingCommandIdWithDifferentPayloadIsRejected()
    {
        var paths = new AppPaths(_directory);
        var exchange = new CompanionExchangeService(paths);
        var commandId = Guid.NewGuid().ToString();

        await exchange.SendAsync(new
        {
            contract = "companion-user-command/v1",
            command_id = commandId,
            cycle_id = "cycle-1",
            type = "begin_voice_capture",
        });

        var error = await Assert.ThrowsAsync<InvalidOperationException>(() => exchange.SendAsync(new
        {
            contract = "companion-user-command/v1",
            command_id = commandId,
            cycle_id = "cycle-2",
            type = "begin_voice_capture",
        }));

        Assert.Contains("conflict", error.Message, StringComparison.OrdinalIgnoreCase);
    }

    [Fact]
    public async Task CompanionCommandsCarryARestartSafeCausalSequencePerCycle()
    {
        var paths = new AppPaths(_directory);
        var first = new CompanionExchangeService(paths);
        await first.SendAsync(new
        {
            contract = "companion-user-command/v1",
            command_id = "stage-1",
            cycle_id = "cycle-causal",
            type = "stage_message",
        });

        var restarted = new CompanionExchangeService(paths);
        await restarted.SendAsync(new
        {
            contract = "companion-user-command/v1",
            command_id = "commit-2",
            cycle_id = "cycle-causal",
            type = "commit_conversation_batch",
        });

        using var stage = JsonDocument.Parse(await File.ReadAllTextAsync(
            Path.Combine(paths.CompanionToRuntimePendingDirectory, "stage-1.json")));
        using var commit = JsonDocument.Parse(await File.ReadAllTextAsync(
            Path.Combine(paths.CompanionToRuntimePendingDirectory, "commit-2.json")));
        Assert.Equal("cycle-causal", stage.RootElement.GetProperty("causal_stream").GetString());
        Assert.Equal(1, stage.RootElement.GetProperty("causal_sequence").GetInt32());
        Assert.Equal(2, commit.RootElement.GetProperty("causal_sequence").GetInt32());
    }

    public void Dispose()
    {
        if (Directory.Exists(_directory)) Directory.Delete(_directory, recursive: true);
    }
}
