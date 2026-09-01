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
        Assert.Contains("Text=\"对话\"", xaml, StringComparison.Ordinal);
        Assert.DoesNotContain("Text=\"AI消息\"", xaml, StringComparison.Ordinal);
        Assert.Contains("MainSendButton", xaml, StringComparison.Ordinal);
        Assert.Contains("MainCommitButton", xaml, StringComparison.Ordinal);
        Assert.Contains("Text=\"我的消息\"", xaml, StringComparison.Ordinal);
        Assert.Contains("Content=\"盘前\" Tag=\"premarket\"", xaml, StringComparison.Ordinal);
        Assert.DoesNotContain("M0Viewer", xaml, StringComparison.Ordinal);
        Assert.DoesNotContain("M1Viewer", xaml, StringComparison.Ordinal);
        Assert.Contains("BasedOn=\"{StaticResource ThinScrollBarStyle}\"", xaml, StringComparison.Ordinal);
    }

    [Fact]
    public void DesktopDoesNotExposeLegacyProviderConfigurationOrQualityControls()
    {
        var root = new DirectoryInfo(AppContext.BaseDirectory);
        while (root is not null && !File.Exists(Path.Combine(root.FullName, "AITradingCompanion.sln")))
            root = root.Parent;
        Assert.NotNull(root);
        var desktop = Path.Combine(root.FullName!, "src", "desktop", "AITradingCompanion.Desktop");
        var source = string.Join("\n", Directory.EnumerateFiles(desktop, "*.*", SearchOption.AllDirectories)
            .Where(path => !path.Contains($"{Path.DirectorySeparatorChar}bin{Path.DirectorySeparatorChar}", StringComparison.OrdinalIgnoreCase))
            .Where(path => !path.Contains($"{Path.DirectorySeparatorChar}obj{Path.DirectorySeparatorChar}", StringComparison.OrdinalIgnoreCase))
            .Where(path => path.EndsWith(".cs", StringComparison.OrdinalIgnoreCase) || path.EndsWith(".xaml", StringComparison.OrdinalIgnoreCase))
            .Select(File.ReadAllText));

        Assert.DoesNotContain("ProviderSettings", source, StringComparison.Ordinal);
        Assert.DoesNotContain("ProviderQuality", source, StringComparison.Ordinal);
        Assert.DoesNotContain("configure-provider", source, StringComparison.Ordinal);
    }

    [Fact]
    public void HistoryListHeaderDoesNotDisplayTheRecordCount()
    {
        var root = new DirectoryInfo(AppContext.BaseDirectory);
        while (root is not null && !File.Exists(Path.Combine(root.FullName, "AITradingCompanion.sln")))
            root = root.Parent;
        Assert.NotNull(root);
        var xaml = File.ReadAllText(Path.Combine(root.FullName!,
            "src", "desktop", "AITradingCompanion.Desktop", "Views", "MainWindow.xaml"));

        Assert.Contains("Text=\"历史列表\"", xaml, StringComparison.Ordinal);
        Assert.DoesNotContain("Binding HistoryCount", xaml, StringComparison.Ordinal);
    }

    [Fact]
    public void SelectedHistoryItemUsesAReadableForegroundAndBackground()
    {
        var root = new DirectoryInfo(AppContext.BaseDirectory);
        while (root is not null && !File.Exists(Path.Combine(root.FullName, "AITradingCompanion.sln"))) root = root.Parent;
        Assert.NotNull(root);
        var xaml = File.ReadAllText(Path.Combine(root.FullName!, "src", "desktop", "AITradingCompanion.Desktop", "Views", "MainWindow.xaml"));

        Assert.Contains("Property=\"IsSelected\" Value=\"True\"", xaml, StringComparison.Ordinal);
        Assert.Contains("Value=\"{StaticResource BlueBrush}\"", xaml, StringComparison.Ordinal);
        Assert.Contains("Foreground\" Value=\"White\"", xaml, StringComparison.Ordinal);
    }

    [Fact]
    public void GatewayHistorySelectionLoadsItsAuthoritativeCycleProjection()
    {
        var root = new DirectoryInfo(AppContext.BaseDirectory);
        while (root is not null && !File.Exists(Path.Combine(root.FullName, "AITradingCompanion.sln"))) root = root.Parent;
        Assert.NotNull(root);
        var code = File.ReadAllText(Path.Combine(root.FullName!,
            "src", "desktop", "AITradingCompanion.Desktop", "Views", "MainWindow.xaml.cs"));

        Assert.Contains("CompanionEventProjection.ProjectForCycle(events, historicalCycleId)", code, StringComparison.Ordinal);
        Assert.Contains("RequestCompanionProjectionAsync(historicalCycleId)", code, StringComparison.Ordinal);
        Assert.Contains("RenderUserMessages();", code, StringComparison.Ordinal);
    }

    [Fact]
    public void TodayTaskRowsUseTheCycleIdentityReturnedByTheRuntimeSnapshot()
    {
        var root = new DirectoryInfo(AppContext.BaseDirectory);
        while (root is not null && !File.Exists(Path.Combine(root.FullName, "AITradingCompanion.sln"))) root = root.Parent;
        Assert.NotNull(root);
        var code = File.ReadAllText(Path.Combine(root.FullName!,
            "src", "desktop", "AITradingCompanion.Desktop", "ViewModels", "MainViewModel.cs"));

        Assert.Contains("_todayCycleIdsByTaskKey[taskKey] = cycleId", code, StringComparison.Ordinal);
        Assert.Contains("_todayCycleIdsByTaskKey.GetValueOrDefault(expected.TaskKey)", code, StringComparison.Ordinal);
    }

    [Fact]
    public void EveryRenderedAiAndUserMessageGetsAnOriginalTextCopyButton()
    {
        var root = new DirectoryInfo(AppContext.BaseDirectory);
        while (root is not null && !File.Exists(Path.Combine(root.FullName, "AITradingCompanion.sln")))
            root = root.Parent;
        Assert.NotNull(root);
        var path = Path.Combine(root.FullName!,
            "src", "desktop", "AITradingCompanion.Desktop", "Views", "MainWindow.xaml.cs");
        var source = File.ReadAllText(path);

        Assert.Contains("CreateCopyButton(message.Text)", source, StringComparison.Ordinal);
        Assert.Contains("CreateCopyButton(entry.Text)", source, StringComparison.Ordinal);
        Assert.Contains("CreatePublishedMessageViewer(message)", source, StringComparison.Ordinal);
        Assert.Contains("CreateMarkdownViewer(entry.Text", source, StringComparison.Ordinal);
        Assert.Contains("MarkdownDocumentBuilder.Build(markdown)", source, StringComparison.Ordinal);
        Assert.Contains("Clipboard.SetText(text)", source, StringComparison.Ordinal);
    }

    [Fact]
    public void CompanionBubblesDoNotExposeInternalStageIdentities()
    {
        var root = new DirectoryInfo(AppContext.BaseDirectory);
        while (root is not null && !File.Exists(Path.Combine(root.FullName, "AITradingCompanion.sln"))) root = root.Parent;
        Assert.NotNull(root);
        var source = File.ReadAllText(Path.Combine(root.FullName!,
            "src", "desktop", "AITradingCompanion.Desktop", "Views", "MainWindow.xaml.cs"));

        Assert.DoesNotContain("M0 · 客观观察", source, StringComparison.Ordinal);
        Assert.DoesNotContain("M1 · 独立判断", source, StringComparison.Ordinal);
        Assert.DoesNotContain("M2 · 伴生综合", source, StringComparison.Ordinal);
        Assert.DoesNotContain("AI 正在回复中", source, StringComparison.Ordinal);
        Assert.Contains("正在想…", source, StringComparison.Ordinal);
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

    [Fact]
    public void TaskManagementWindowIsNonModalAndKeepsTechnicalDetailsOutOfTheUi()
    {
        var root = new DirectoryInfo(AppContext.BaseDirectory);
        while (root is not null && !File.Exists(Path.Combine(root.FullName, "AITradingCompanion.sln"))) root = root.Parent;
        Assert.NotNull(root);
        var xaml = File.ReadAllText(Path.Combine(root.FullName!, "src", "desktop", "AITradingCompanion.Desktop", "Views", "TaskManagementWindow.xaml"));
        var main = File.ReadAllText(Path.Combine(root.FullName!, "src", "desktop", "AITradingCompanion.Desktop", "Views", "MainWindow.xaml.cs"));
        Assert.Contains("任务管理", xaml, StringComparison.Ordinal);
        Assert.Contains("ThinScrollBarStyle", xaml, StringComparison.Ordinal);
        Assert.DoesNotContain("workflow_key", xaml, StringComparison.Ordinal);
        Assert.Contains("TaskButton_Click", main, StringComparison.Ordinal);
        Assert.Contains("_taskManagementWindow.Show()", main, StringComparison.Ordinal);
        Assert.Contains("Content=\"任务管理\"", File.ReadAllText(Path.Combine(root.FullName!, "src", "desktop", "AITradingCompanion.Desktop", "Views", "MainWindow.xaml")), StringComparison.Ordinal);
        Assert.Contains("ArchiveButton", xaml, StringComparison.Ordinal);
            Assert.DoesNotContain("运行记录", xaml, StringComparison.Ordinal);
            Assert.DoesNotContain("<TabControl Grid.Column=\"2\"", xaml, StringComparison.Ordinal);
            Assert.DoesNotContain("<TextBlock Text=\"设置\"", xaml, StringComparison.Ordinal);
            Assert.Contains("StackPanel Orientation=\"Horizontal\"", xaml, StringComparison.Ordinal);
        Assert.Contains("FormatNext", File.ReadAllText(Path.Combine(root.FullName!, "src", "desktop", "AITradingCompanion.Desktop", "Views", "TaskManagementWindow.xaml.cs")), StringComparison.Ordinal);
        var taskWindowCode = File.ReadAllText(Path.Combine(root.FullName!, "src", "desktop", "AITradingCompanion.Desktop", "Views", "TaskManagementWindow.xaml.cs"));
        Assert.Contains("下次触发：", taskWindowCode, StringComparison.Ordinal);
        Assert.Contains("events.Reverse()", taskWindowCode, StringComparison.Ordinal);
        Assert.Contains("OrderBy(item => item.NextAt ?? DateTimeOffset.MaxValue)", taskWindowCode, StringComparison.Ordinal);
        Assert.DoesNotContain(": Trigger;", taskWindowCode, StringComparison.Ordinal);
        Assert.Contains("\"calendar_periodic\" => \"日历周期\"", taskWindowCode, StringComparison.Ordinal);
        Assert.Contains("\"market_relative\" => \"开收盘相对时间\"", taskWindowCode, StringComparison.Ordinal);
        Assert.Contains("\"once\" => \"单次任务\"", taskWindowCode, StringComparison.Ordinal);
        Assert.Contains("x:Name=\"HourBox\"", xaml, StringComparison.Ordinal);
        Assert.Contains("x:Name=\"MinuteBox\"", xaml, StringComparison.Ordinal);
        Assert.Contains("x:Name=\"OnceDatePicker\"", xaml, StringComparison.Ordinal);
        Assert.Contains("x:Name=\"StartDatePicker\"", xaml, StringComparison.Ordinal);
        Assert.Contains("x:Name=\"EndDatePicker\"", xaml, StringComparison.Ordinal);
        Assert.Contains("Style TargetType=\"DatePickerTextBox\"", xaml, StringComparison.Ordinal);
        Assert.Contains("Style TargetType=\"Calendar\"", xaml, StringComparison.Ordinal);
        Assert.Contains("Style TargetType=\"CalendarItem\"", xaml, StringComparison.Ordinal);
        Assert.Contains("Style TargetType=\"CalendarButton\"", xaml, StringComparison.Ordinal);
        Assert.Contains("Style TargetType=\"CalendarDayButton\"", xaml, StringComparison.Ordinal);
        Assert.DoesNotContain("x:Name=\"TimeBox\"", xaml, StringComparison.Ordinal);
        Assert.DoesNotContain("x:Name=\"StartDateBox\"", xaml, StringComparison.Ordinal);
        Assert.Contains("SelectedDate", taskWindowCode, StringComparison.Ordinal);
        var mainViewModel = File.ReadAllText(Path.Combine(root.FullName!, "src", "desktop", "AITradingCompanion.Desktop", "ViewModels", "MainViewModel.cs"));
        Assert.DoesNotContain("· {SelectedMessage.Source}", mainViewModel, StringComparison.Ordinal);
        Assert.Contains("SourceTextFor", mainViewModel, StringComparison.Ordinal);
    }

    [Fact]
    public void DesktopUsesRuntimeTradingDayProjectionForTodaysTaskList()
    {
        var root = new DirectoryInfo(AppContext.BaseDirectory);
        while (root is not null && !File.Exists(Path.Combine(root.FullName, "AITradingCompanion.sln"))) root = root.Parent;
        Assert.NotNull(root);
        var main = File.ReadAllText(Path.Combine(root.FullName!, "src", "desktop", "AITradingCompanion.Desktop", "Views", "MainWindow.xaml.cs"));
        var viewModel = File.ReadAllText(Path.Combine(root.FullName!, "src", "desktop", "AITradingCompanion.Desktop", "ViewModels", "MainViewModel.cs"));

        Assert.Contains("RefreshTodayBoardAsync", main, StringComparison.Ordinal);
        Assert.Contains("GetSnapshotAsync(\"today\"", viewModel, StringComparison.Ordinal);
        Assert.Contains("is_trading_day", viewModel, StringComparison.Ordinal);
        Assert.Contains("ForTradingDay", viewModel, StringComparison.Ordinal);
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
