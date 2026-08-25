using System.Diagnostics;

namespace AIDecisionCenter.App.Services;

public static class CompanionRuntimeService
{
    public static void EnsureStarted()
    {
        var projectRoot = Environment.GetEnvironmentVariable("STOCK_ADVISOR_PROJECT_ROOT")
            ?? @"D:\WILL\STOCK\stock_advisor";
        var script = Path.Combine(projectRoot, "scripts", "run_companion_service.ps1");
        if (!File.Exists(script))
        {
            throw new FileNotFoundException("伴生运行服务脚本不可读。", script);
        }
        var heartbeat = Path.Combine(projectRoot, "data", "runtime", "companion", "service-heartbeat.json");
        if (File.Exists(heartbeat) && File.GetLastWriteTimeUtc(heartbeat) >= DateTime.UtcNow.AddSeconds(-30))
        {
            return;
        }

        var start = new ProcessStartInfo("powershell.exe")
        {
            WorkingDirectory = projectRoot,
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
