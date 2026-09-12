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
        await restarted.SendAsync(new
        {
            contract = "companion-user-command/v1",
            command_id = "edit-3",
            cycle_id = "cycle-causal",
            type = "edit_staged_message",
        });
        await restarted.SendAsync(new
        {
            contract = "companion-user-command/v1",
            command_id = "withdraw-4",
            cycle_id = "cycle-causal",
            type = "withdraw_staged_message",
        });

        using var stage = JsonDocument.Parse(await File.ReadAllTextAsync(
            Path.Combine(paths.CompanionToRuntimePendingDirectory, "stage-1.json")));
        using var commit = JsonDocument.Parse(await File.ReadAllTextAsync(
            Path.Combine(paths.CompanionToRuntimePendingDirectory, "commit-2.json")));
        using var edit = JsonDocument.Parse(await File.ReadAllTextAsync(
            Path.Combine(paths.CompanionToRuntimePendingDirectory, "edit-3.json")));
        using var withdraw = JsonDocument.Parse(await File.ReadAllTextAsync(
            Path.Combine(paths.CompanionToRuntimePendingDirectory, "withdraw-4.json")));
        Assert.Equal("cycle-causal", stage.RootElement.GetProperty("causal_stream").GetString());
        Assert.Equal(1, stage.RootElement.GetProperty("causal_sequence").GetInt32());
        Assert.Equal(2, commit.RootElement.GetProperty("causal_sequence").GetInt32());
        Assert.Equal(3, edit.RootElement.GetProperty("causal_sequence").GetInt32());
        Assert.Equal(4, withdraw.RootElement.GetProperty("causal_sequence").GetInt32());
    }

    [Fact]
    public async Task CausalSequenceSurvivesExchangeStateCompactionAndRemainsPerCycle()
    {
        var paths = new AppPaths(_directory);
        var exchange = new CompanionExchangeService(paths);

        await exchange.SendAsync(new
        {
            contract = "companion-user-command/v1",
            command_id = "cycle-a-1",
            cycle_id = "cycle-a",
            type = "stage_message",
        });
        foreach (var state in new[] { "pending", "processing", "processed", "dead-letter" })
        {
            var directory = Path.Combine(paths.CompanionExchangeRoot, "to-runtime", state);
            if (!Directory.Exists(directory)) continue;
            foreach (var file in Directory.EnumerateFiles(directory, "*.json")) File.Delete(file);
        }

        await exchange.SendAsync(new
        {
            contract = "companion-user-command/v1",
            command_id = "cycle-a-2",
            cycle_id = "cycle-a",
            type = "commit_h0",
        });
        await exchange.SendAsync(new
        {
            contract = "companion-user-command/v1",
            command_id = "cycle-b-1",
            cycle_id = "cycle-b",
            type = "stage_message",
        });

        using var second = JsonDocument.Parse(await File.ReadAllTextAsync(
            Path.Combine(paths.CompanionToRuntimePendingDirectory, "cycle-a-2.json")));
        using var other = JsonDocument.Parse(await File.ReadAllTextAsync(
            Path.Combine(paths.CompanionToRuntimePendingDirectory, "cycle-b-1.json")));
        Assert.Equal(2, second.RootElement.GetProperty("causal_sequence").GetInt32());
        Assert.Equal(1, other.RootElement.GetProperty("causal_sequence").GetInt32());
    }

    public void Dispose()
    {
        if (Directory.Exists(_directory)) Directory.Delete(_directory, recursive: true);
    }
}
