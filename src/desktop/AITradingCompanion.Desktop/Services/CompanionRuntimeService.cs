using System.Diagnostics;

namespace AITradingCompanion.Desktop.Services;

public static class CompanionRuntimeService
{
    public static void EnsureStarted()
    {
        var installRoot = Environment.GetEnvironmentVariable("AI_TRADING_COMPANION_INSTALL_ROOT")
            ?? AppContext.BaseDirectory;
        var script = Path.Combine(installRoot, "scripts", "run_companion_service.ps1");
        if (!File.Exists(script))
        {
            throw new FileNotFoundException("伴生运行服务脚本不可读。", script);
        }
        var dataRoot = Environment.GetEnvironmentVariable("AI_TRADING_COMPANION_HOME")
            ?? Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "AITradingCompanion");
        var heartbeat = Path.Combine(dataRoot, "runtime", "service-heartbeat.json");
        if (File.Exists(heartbeat) && File.GetLastWriteTimeUtc(heartbeat) >= DateTime.UtcNow.AddSeconds(-30))
        {
            return;
        }

        var start = new ProcessStartInfo("powershell.exe")
        {
            WorkingDirectory = installRoot,
            UseShellExecute = false,
            CreateNoWindow = true,
            WindowStyle = ProcessWindowStyle.Hidden
        };
        start.ArgumentList.Add("-NoProfile");
        start.ArgumentList.Add("-ExecutionPolicy");
        start.ArgumentList.Add("Bypass");
        start.ArgumentList.Add("-File");
        start.ArgumentList.Add(script);
        start.ArgumentList.Add("-Execute");
        start.ArgumentList.Add("-PollSeconds");
        start.ArgumentList.Add("5");
        Process.Start(start)?.Dispose();
    }
}
