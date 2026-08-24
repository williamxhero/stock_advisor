using System.Windows;
using AIDecisionCenter.App.Services;
using AIDecisionCenter.App.ViewModels;
using AIDecisionCenter.App.Views;

namespace AIDecisionCenter.App;

public partial class App : System.Windows.Application, IDisposable
{
    private readonly CancellationTokenSource _shutdown = new();
    private DesktopNotificationService? _notifications;
    private GmailMessageSource? _gmail;
    private bool _disposed;

    protected override async void OnStartup(StartupEventArgs e)
    {
        base.OnStartup(e);

        var paths = new AppPaths();
        var settings = await new SettingsService(paths).LoadAsync().ConfigureAwait(true);
        var store = new SqliteTaskMessageStore(paths);
        _gmail = new GmailMessageSource(paths);
        var sync = new TaskSyncService(_gmail, store);

        MainWindow? window = null;
        _notifications = new DesktopNotificationService(() =>
        {
            if (window is null)
            {
                return;
            }

            window.Show();
            window.WindowState = WindowState.Normal;
            window.Activate();
        });

        var viewModel = new MainViewModel(store, sync, _notifications, paths, settings);
        window = new MainWindow(viewModel);
        MainWindow = window;
        window.Show();
        await viewModel.InitializeAsync().ConfigureAwait(true);
        _ = PollAsync(viewModel, _shutdown.Token);
    }

    protected override void OnExit(ExitEventArgs e)
    {
        Dispose();
        base.OnExit(e);
    }

    public void Dispose()
    {
        if (_disposed)
        {
            return;
        }

        _disposed = true;
        _shutdown.Cancel();
        _notifications?.Dispose();
        _gmail?.Dispose();
        _shutdown.Dispose();
        GC.SuppressFinalize(this);
    }

    private static async Task PollAsync(MainViewModel viewModel, CancellationToken cancellationToken)
    {
        while (!cancellationToken.IsCancellationRequested)
        {
            try
            {
                var delay = await Current.Dispatcher
                    .InvokeAsync(
                        () => viewModel.GetNextPollDelay(DateTime.Now),
                        System.Windows.Threading.DispatcherPriority.Background,
                        cancellationToken)
                    .Task
                    .ConfigureAwait(false);
                await Task.Delay(delay, cancellationToken).ConfigureAwait(false);
                if (delay > TimeSpan.Zero)
                {
                    continue;
                }

                await Current.Dispatcher
                    .InvokeAsync(viewModel.PollOnceAsync, System.Windows.Threading.DispatcherPriority.Background, cancellationToken)
                    .Task
                    .Unwrap()
                    .ConfigureAwait(false);
            }
            catch (OperationCanceledException) when (cancellationToken.IsCancellationRequested)
            {
                break;
            }
        }
    }
}
