using System.Windows;

namespace AITradingCompanion.ToolManager;

public partial class App : Application, IDisposable
{
    private const string MutexName = "Local\\AITradingCompanion.ToolManager";
    private const string ActivateEventName = "Local\\AITradingCompanion.ToolManager.Activate";
    private Mutex? _mutex;
    private EventWaitHandle? _activate;

    protected override void OnStartup(StartupEventArgs e)
    {
        _mutex = new Mutex(true, MutexName, out var created);
        if (!created)
        {
            EventWaitHandle.OpenExisting(ActivateEventName).Set();
            Shutdown();
            return;
        }
        _activate = new EventWaitHandle(false, EventResetMode.AutoReset, ActivateEventName);
        var window = new MainWindow(new ToolManagerProjectionReader());
        _ = Task.Run(() =>
        {
            while (_activate.WaitOne())
            {
                Dispatcher.Invoke(() => { window.Show(); window.WindowState = WindowState.Normal; window.Activate(); });
            }
        });
        MainWindow = window;
        window.Show();
        base.OnStartup(e);
    }

    protected override void OnExit(ExitEventArgs e)
    {
        Dispose();
        base.OnExit(e);
    }

    public void Dispose()
    {
        _activate?.Set(); _activate?.Dispose(); _activate = null;
        if (_mutex is not null) { _mutex.ReleaseMutex(); _mutex.Dispose(); _mutex = null; }
        GC.SuppressFinalize(this);
    }
}
