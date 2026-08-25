using System.Text;
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

    public void Dispose()
    {
        if (Directory.Exists(_directory)) Directory.Delete(_directory, recursive: true);
    }
}
