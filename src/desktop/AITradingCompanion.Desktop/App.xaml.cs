using System.Windows;
using AITradingCompanion.Desktop.Services;
using AITradingCompanion.Desktop.ViewModels;
using AITradingCompanion.Desktop.Views;

namespace AITradingCompanion.Desktop;

public partial class App : System.Windows.Application, IDisposable
{
    private readonly CancellationTokenSource _shutdown = new();
    private DesktopNotificationService? _notifications;
    private LocalInboxService? _inbox;
    private bool _disposed;

    protected override async void OnStartup(StartupEventArgs e)
    {
        base.OnStartup(e);

        var paths = new AppPaths();
        var settings = await new SettingsService(paths).LoadAsync().ConfigureAwait(true);
        var store = new SqliteTaskMessageStore(paths);
        _inbox = new LocalInboxService(paths, store, settings);

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

        var viewModel = new MainViewModel(store, _inbox, _notifications, paths, settings, new LoopbackHttpGateway(paths));
        try
        {
            CompanionRuntimeService.EnsureStarted();
        }
        catch (Exception exception)
        {
            viewModel.ReportInboxFailure(exception);
        }
        window = new MainWindow(viewModel, paths);
        MainWindow = window;
        window.Show();
        await viewModel.InitializeAsync().ConfigureAwait(true);
        _ = RunInboxAsync(viewModel, _inbox, _shutdown.Token);
        _ = RefreshClockAsync(viewModel, _shutdown.Token);
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
        _inbox?.Dispose();
        _shutdown.Dispose();
        GC.SuppressFinalize(this);
    }

    private static async Task RunInboxAsync(
        MainViewModel viewModel,
        LocalInboxService inbox,
        CancellationToken cancellationToken)
    {
        try
        {
            await inbox.RunAsync(
                batch => Current.Dispatcher.InvokeAsync(
                    () => viewModel.HandleImportBatchAsync(batch),
                    System.Windows.Threading.DispatcherPriority.Background,
                    cancellationToken).Task.Unwrap(),
                cancellationToken).ConfigureAwait(false);
        }
        catch (OperationCanceledException) when (cancellationToken.IsCancellationRequested)
        {
        }
        catch (Exception exception)
        {
            await Current.Dispatcher.InvokeAsync(
                () => viewModel.ReportInboxFailure(exception),
                System.Windows.Threading.DispatcherPriority.Background,
                cancellationToken);
        }
    }

    private static async Task RefreshClockAsync(MainViewModel viewModel, CancellationToken cancellationToken)
    {
        while (!cancellationToken.IsCancellationRequested)
        {
            try
            {
                await Task.Delay(TimeSpan.FromMinutes(1), cancellationToken).ConfigureAwait(false);
                await Current.Dispatcher.InvokeAsync(
                    () => viewModel.RefreshTaskStatuses(DateTime.Now),
                    System.Windows.Threading.DispatcherPriority.Background,
                    cancellationToken);
            }
            catch (OperationCanceledException) when (cancellationToken.IsCancellationRequested)
            {
                break;
            }
        }
    }
}
