using AITradingCompanion.Desktop.Services;

namespace AITradingCompanion.Tests;

public sealed class NotificationAlertDispatcherTests
{
    [Fact]
    public void PlaysSoundAfterShowingNotification()
    {
        var events = new List<string>();
        var dispatcher = new NotificationAlertDispatcher(
            (_, _) => events.Add("notification"),
            () => events.Add("sound"));

        dispatcher.Show("title", "message");

        Assert.Equal(["notification", "sound"], events);
    }
}
