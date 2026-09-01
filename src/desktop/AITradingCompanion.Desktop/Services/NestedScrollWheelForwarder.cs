using System.Windows;
using System.Windows.Controls;
using System.Windows.Input;
using System.Windows.Media;

namespace AITradingCompanion.Desktop.Services;

internal static class NestedScrollWheelForwarder
{
    public static void Attach(FrameworkElement element)
    {
        element.PreviewMouseWheel -= ForwardToParentScrollViewer;
        element.PreviewMouseWheel += ForwardToParentScrollViewer;
    }

    private static void ForwardToParentScrollViewer(object sender, MouseWheelEventArgs eventArgs)
    {
        if (sender is not DependencyObject source || FindParentScrollViewer(source) is not { } parent) return;
        var forwarded = new MouseWheelEventArgs(eventArgs.MouseDevice, eventArgs.Timestamp, eventArgs.Delta)
        {
            RoutedEvent = Mouse.MouseWheelEvent,
            Source = parent,
        };
        parent.RaiseEvent(forwarded);
        eventArgs.Handled = true;
    }

    private static ScrollViewer? FindParentScrollViewer(DependencyObject source)
    {
        for (var parent = VisualTreeHelper.GetParent(source); parent is not null; parent = VisualTreeHelper.GetParent(parent))
        {
            if (parent is ScrollViewer scrollViewer) return scrollViewer;
        }
        return null;
    }
}
