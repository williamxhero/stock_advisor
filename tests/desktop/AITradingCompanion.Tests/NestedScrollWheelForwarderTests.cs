using System.Windows;
using System.Windows.Controls;
using System.Windows.Input;
using AITradingCompanion.Desktop.Services;

namespace AITradingCompanion.Tests;

public sealed class NestedScrollWheelForwarderTests
{
    [Fact]
    public void MarkdownWheelMovesTheOuterMessageTimeline()
    {
        Exception? failure = null;
        var thread = new Thread(() =>
        {
            Window? window = null;
            try
            {
                var markdown = new FlowDocumentScrollViewer
                {
                    Height = 600,
                    VerticalScrollBarVisibility = ScrollBarVisibility.Hidden,
                    HorizontalScrollBarVisibility = ScrollBarVisibility.Disabled,
                };
                NestedScrollWheelForwarder.Attach(markdown);
                var panel = new StackPanel { Children = { markdown, new Border { Height = 600 } } };
                var timeline = new ScrollViewer
                {
                    Width = 360,
                    Height = 180,
                    VerticalScrollBarVisibility = ScrollBarVisibility.Auto,
                    Content = panel,
                };
                window = new Window
                {
                    Width = 400,
                    Height = 240,
                    ShowInTaskbar = false,
                    WindowStyle = WindowStyle.None,
                    Content = timeline,
                };
                window.Show();
                window.UpdateLayout();
                timeline.ScrollToVerticalOffset(100);
                window.UpdateLayout();
                var before = timeline.VerticalOffset;

                var wheel = new MouseWheelEventArgs(Mouse.PrimaryDevice, Environment.TickCount, -120)
                {
                    RoutedEvent = Mouse.PreviewMouseWheelEvent,
                    Source = markdown,
                };
                markdown.RaiseEvent(wheel);
                window.UpdateLayout();

                Assert.True(timeline.VerticalOffset > before,
                    $"Expected the outer timeline to move beyond {before}, but it stayed at {timeline.VerticalOffset}.");
            }
            catch (Exception exception) { failure = exception; }
            finally { window?.Close(); }
        });
        thread.SetApartmentState(ApartmentState.STA);
        thread.Start();
        thread.Join();
        if (failure is not null) throw failure;
    }
}
