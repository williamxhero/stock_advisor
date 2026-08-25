using System.Drawing;
using System.Media;
using System.Windows.Forms;

namespace AITradingCompanion.Desktop.Services;

public sealed class DesktopNotificationService : IDisposable
{
    private readonly Icon _appIcon;
    private readonly NotifyIcon _notifyIcon;
    private readonly NotificationAlertDispatcher _alertDispatcher;
    private readonly Action _showWindow;

    public DesktopNotificationService(Action showWindow)
    {
        _showWindow = showWindow;
        _appIcon = Environment.ProcessPath is { Length: > 0 } processPath
            ? Icon.ExtractAssociatedIcon(processPath) ?? (Icon)SystemIcons.Information.Clone()
            : (Icon)SystemIcons.Information.Clone();
        _notifyIcon = new NotifyIcon
        {
            Icon = _appIcon,
            Text = "AI交易伙伴",
            Visible = true
        };
        _notifyIcon.DoubleClick += OnDoubleClick;
        _alertDispatcher = new NotificationAlertDispatcher(ShowBalloon, SystemSounds.Exclamation.Play);
    }

    public void Show(string title, string message) => _alertDispatcher.Show(title, message);

    private void ShowBalloon(string title, string message)
    {
        _notifyIcon.BalloonTipTitle = title;
        _notifyIcon.BalloonTipText = message;
        _notifyIcon.BalloonTipIcon = ToolTipIcon.Info;
        _notifyIcon.ShowBalloonTip(6000);
    }

    public void Dispose()
    {
        _notifyIcon.DoubleClick -= OnDoubleClick;
        _notifyIcon.Visible = false;
        _notifyIcon.Dispose();
        _appIcon.Dispose();
    }

    private void OnDoubleClick(object? sender, EventArgs e) => _showWindow();
}
