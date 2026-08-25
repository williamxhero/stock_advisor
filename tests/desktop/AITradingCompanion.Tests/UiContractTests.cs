using System.Windows;
using System.Windows.Controls;
using System.Windows.Controls.Primitives;
using System.Windows.Media;

namespace AITradingCompanion.Tests;

public sealed class UiContractTests
{
    [Fact]
    public void ThinScrollbarStyleIsTwoDipTransparentAndHasNoDirectionButtons()
    {
        Exception? failure = null;
        var thread = new Thread(() =>
        {
            try
            {
                var app = EnsureApplication();
                var style = Assert.IsType<Style>(app.Resources["ThinScrollBarStyle"]);
                var vertical = new ScrollBar { Style = style, Orientation = Orientation.Vertical };
                vertical.Measure(new Size(20, 200));
                vertical.Arrange(new Rect(0, 0, 2, 200));
                vertical.ApplyTemplate();
                Assert.Equal(2d, vertical.Width);
                Assert.Equal(Brushes.Transparent, vertical.Background);
                Assert.Empty(Descendants<RepeatButton>(vertical));
                var verticalTrack = Assert.IsType<Track>(vertical.Template.FindName("PART_Track", vertical));
                Assert.Equal(24d, verticalTrack.Thumb.MinHeight);

                var horizontal = new ScrollBar { Style = style, Orientation = Orientation.Horizontal };
                horizontal.Measure(new Size(200, 20));
                horizontal.Arrange(new Rect(0, 0, 200, 2));
                horizontal.ApplyTemplate();
                Assert.Equal(2d, horizontal.Height);
                Assert.Empty(Descendants<RepeatButton>(horizontal));
                var horizontalTrack = Assert.IsType<Track>(horizontal.Template.FindName("PART_Track", horizontal));
                Assert.Equal(24d, horizontalTrack.Thumb.MinWidth);
                Assert.Equal(0d, horizontalTrack.Thumb.MinHeight);
                var viewer = new ScrollViewer
                {
                    Width = 120,
                    Height = 80,
                    VerticalScrollBarVisibility = ScrollBarVisibility.Visible,
                    HorizontalScrollBarVisibility = ScrollBarVisibility.Visible,
                    Content = new Border { Width = 400, Height = 400 },
                };
                var list = new ListBox { Width = 120, Height = 80 };
                foreach (var item in Enumerable.Range(1, 40)) list.Items.Add($"item {item}");
                var text = new TextBox
                {
                    Width = 120, Height = 80, AcceptsReturn = true,
                    VerticalScrollBarVisibility = ScrollBarVisibility.Visible,
                    Text = string.Join(Environment.NewLine, Enumerable.Range(1, 40).Select(item => $"line {item}")),
                };
                var panel = new StackPanel { Children = { viewer, list, text } };
                var window = new Window { Content = panel, Width = 180, Height = 320, ShowInTaskbar = false };
                window.Show();
                window.UpdateLayout();

                var scrollbars = Descendants<ScrollBar>(viewer).ToArray();
                var internalVertical = Assert.Single(scrollbars, item => item.Orientation == Orientation.Vertical);
                var internalHorizontal = Assert.Single(scrollbars, item => item.Orientation == Orientation.Horizontal);
                Assert.Equal(2d, internalVertical.ActualWidth);
                Assert.Equal(2d, internalHorizontal.ActualHeight);
                Assert.Empty(Descendants<RepeatButton>(internalVertical));
                Assert.Empty(Descendants<RepeatButton>(internalHorizontal));
                viewer.ScrollToVerticalOffset(40);
                viewer.ScrollToHorizontalOffset(30);
                window.UpdateLayout();
                Assert.True(viewer.VerticalOffset > 0);
                Assert.True(viewer.HorizontalOffset > 0);
                Assert.Equal(viewer.VerticalOffset, internalVertical.Value);
                Assert.Equal(viewer.HorizontalOffset, internalHorizontal.Value);
                var allInternal = Descendants<ScrollBar>(panel).ToArray();
                Assert.True(allInternal.Length >= 4);
                Assert.All(allInternal.Where(item => item.Orientation == Orientation.Vertical), item => Assert.Equal(2d, item.Width));
                Assert.All(allInternal.Where(item => item.Orientation == Orientation.Horizontal), item => Assert.Equal(2d, item.Height));
                Assert.All(allInternal.Where(item => item.IsVisible && item.Orientation == Orientation.Vertical), item => Assert.Equal(2d, item.ActualWidth));
                Assert.All(allInternal.Where(item => item.IsVisible && item.Orientation == Orientation.Horizontal), item => Assert.Equal(2d, item.ActualHeight));
                Assert.All(allInternal, item => Assert.Empty(Descendants<RepeatButton>(item)));
                window.Close();
            }
            catch (Exception exception) { failure = exception; }
        });
        thread.SetApartmentState(ApartmentState.STA);
        thread.Start();
        thread.Join();
        if (failure is not null) throw failure;
    }

    [Fact]
    public void MainWorkspaceUsesOneAiTimelineAndTwoPhaseMessageControls()
    {
        var root = new DirectoryInfo(AppContext.BaseDirectory);
        while (root is not null && !File.Exists(Path.Combine(root.FullName, "AITradingCompanion.sln")))
            root = root.Parent;
        Assert.NotNull(root);
        var path = Path.Combine(root.FullName,
            "src", "desktop", "AITradingCompanion.Desktop", "Views", "MainWindow.xaml");
        var xaml = File.ReadAllText(path);

        Assert.Contains("AiTimelineScrollViewer", xaml, StringComparison.Ordinal);
        Assert.Contains("AiTimelinePanel", xaml, StringComparison.Ordinal);
        Assert.Contains("MainSendButton", xaml, StringComparison.Ordinal);
        Assert.Contains("MainCommitButton", xaml, StringComparison.Ordinal);
        Assert.Contains("Text=\"我的消息\"", xaml, StringComparison.Ordinal);
        Assert.DoesNotContain("M0Viewer", xaml, StringComparison.Ordinal);
        Assert.DoesNotContain("M1Viewer", xaml, StringComparison.Ordinal);
        Assert.Contains("BasedOn=\"{StaticResource ThinScrollBarStyle}\"", xaml, StringComparison.Ordinal);
    }

    [Fact]
    public void PortfolioWindowIsFactualAndDoesNotRestoreAiStockStatePanel()
    {
        var root = new DirectoryInfo(AppContext.BaseDirectory);
        while (root is not null && !File.Exists(Path.Combine(root.FullName, "AITradingCompanion.sln")))
            root = root.Parent;
        Assert.NotNull(root);
        var path = Path.Combine(root.FullName!, "src", "desktop", "AITradingCompanion.Desktop", "Views", "PortfolioWindow.xaml");
        var xaml = File.ReadAllText(path);

        Assert.DoesNotContain("AI 状态", xaml, StringComparison.Ordinal);
        Assert.DoesNotContain("SelectionChanged=", xaml, StringComparison.Ordinal);
        Assert.Contains("ThinScrollBarStyle", xaml, StringComparison.Ordinal);
    }

    private static IEnumerable<T> Descendants<T>(DependencyObject root) where T : DependencyObject
    {
        for (var index = 0; index < VisualTreeHelper.GetChildrenCount(root); index++)
        {
            var child = VisualTreeHelper.GetChild(root, index);
            if (child is T match) yield return match;
            foreach (var nested in Descendants<T>(child)) yield return nested;
        }
    }

    private static AITradingCompanion.Desktop.App EnsureApplication()
    {
        if (Application.Current is AITradingCompanion.Desktop.App current) return current;
        var app = new AITradingCompanion.Desktop.App();
        app.InitializeComponent();
        return app;
    }
}
