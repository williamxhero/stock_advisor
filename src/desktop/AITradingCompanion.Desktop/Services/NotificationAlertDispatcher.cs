namespace AITradingCompanion.Desktop.Services;

internal sealed class NotificationAlertDispatcher
{
    private readonly Action<string, string> _showNotification;
    private readonly Action _playSound;

    public NotificationAlertDispatcher(Action<string, string> showNotification, Action playSound)
    {
        _showNotification = showNotification;
        _playSound = playSound;
    }

    public void Show(string title, string message)
    {
        _showNotification(title, message);
        _playSound();
    }
}
