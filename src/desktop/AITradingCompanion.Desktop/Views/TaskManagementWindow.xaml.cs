using System.Text.Json;
using System.Globalization;
using System.Windows;
using System.Windows.Controls;
using AITradingCompanion.Desktop.Services;

namespace AITradingCompanion.Desktop.Views;

public partial class TaskManagementWindow : Window
{
    private readonly AppPaths _paths;
    private readonly CompanionExchangeService _exchange;
    private readonly List<ScheduleItem> _items = [];
    private ScheduleItem? _selected;
    private object? _pendingSave;

    public TaskManagementWindow(AppPaths paths, CompanionExchangeService exchange)
    {
        InitializeComponent(); InitializeTimePicker(); _paths = paths; _exchange = exchange;
        Loaded += async (_, _) => { RestoreWindowBounds(); await SendAsync("schedule.list"); };
        Closed += (_, _) => SaveWindowBounds();
    }

    public void UpdateEvents(IReadOnlyList<string> events)
    {
        // Exchange events arrive newest-first. Replay them chronologically so an
        // older schedule.list result cannot overwrite the current projection.
        foreach (var raw in events.Reverse())
        {
            try
            {
                using var document = JsonDocument.Parse(raw); var root = document.RootElement;
                if (root.GetProperty("contract").GetString() != "schedule-client-event/v1") continue;
                var payload = root.GetProperty("payload");
                if (payload.TryGetProperty("schedules", out var schedules)) LoadSchedules(schedules);
                if (payload.TryGetProperty("schedule", out var schedule)) { _selected = ScheduleItem.From(schedule); _pendingSave = null; SaveButton.Content = "保存修改"; Apply(_selected); _ = SendAsync("schedule.list"); }
                if (payload.TryGetProperty("summary", out var summary)) { PreviewText.Text = summary.GetString() ?? ""; SaveButton.Content = "确认保存"; }
            }
            catch (JsonException) { /* unrelated/partial client event */ }
        }
    }

    private async Task SendAsync(string type, object? config = null, string? scheduleId = null, int? version = null)
    {
        var command = new Dictionary<string, object?> { ["contract"] = "schedule-user-command/v1", ["command_id"] = Guid.NewGuid().ToString("N"), ["type"] = type };
        if (config is not null) command["config"] = config;
        if (scheduleId is not null) command["schedule_id"] = scheduleId;
        if (version is not null) command["expected_version"] = version;
        await _exchange.SendAsync(command);
    }

    private void LoadSchedules(JsonElement schedules)
    {
        _items.Clear();
        _items.AddRange(schedules.EnumerateArray()
            .Select(ScheduleItem.From)
            .OrderBy(item => item.NextAt ?? DateTimeOffset.MaxValue)
            .ThenBy(item => item.Id, StringComparer.Ordinal));
        TaskList.ItemsSource = null; TaskList.ItemsSource = _items;
        if (_selected is not null) TaskList.SelectedItem = _items.FirstOrDefault(item => item.Id == _selected.Id);
    }

    private void Apply(ScheduleItem item)
    {
        _pendingSave = null; SaveButton.Content = "保存修改"; _selected = item; NameBox.Text = item.Name; NoteBox.Text = item.Note; SelectDate(StartDatePicker, item.EffectiveFrom); SelectDate(EndDatePicker, item.EffectiveUntil); SelectByTag(WorkflowBox, item.Workflow); SelectByTag(TriggerBox, item.Trigger);
        SelectTime(item.Time); SelectDate(OnceDatePicker, item.TriggerDate); UpdateTriggerControls(); UpdateRuleText(); PauseButton.Content = item.Status == "active" ? "停用任务" : "恢复任务";
        PreviewText.Text = $"{item.Session} · {item.Next}";
    }
    private static void SelectByTag(System.Windows.Controls.ComboBox box, string tag) => box.SelectedValue = tag;
    private static string SelectedTag(System.Windows.Controls.ComboBox box) => box.SelectedValue as string ?? "";
    private void InitializeTimePicker()
    {
        foreach (var hour in Enumerable.Range(0, 24)) HourBox.Items.Add(hour.ToString("00", CultureInfo.InvariantCulture));
        foreach (var minute in Enumerable.Range(0, 60)) MinuteBox.Items.Add(minute.ToString("00", CultureInfo.InvariantCulture));
        SelectTime("09:00");
    }
    private string SelectedTime()
    {
        var hour = HourBox.SelectedItem as string;
        var minute = MinuteBox.SelectedItem as string;
        if (hour is null || minute is null) throw new InvalidOperationException("请选择触发时间");
        return $"{hour}:{minute}";
    }
    private void SelectTime(string value)
    {
        var parts = value.Split(':', 2);
        HourBox.SelectedItem = parts.Length == 2 && int.TryParse(parts[0], out var hour) && hour is >= 0 and <= 23 ? hour.ToString("00", CultureInfo.InvariantCulture) : "09";
        MinuteBox.SelectedItem = parts.Length == 2 && int.TryParse(parts[1], out var minute) && minute is >= 0 and <= 59 ? minute.ToString("00", CultureInfo.InvariantCulture) : "00";
    }
    private static void SelectDate(DatePicker picker, string value) => picker.SelectedDate = DateTime.TryParseExact(value, "yyyy-MM-dd", CultureInfo.InvariantCulture, DateTimeStyles.None, out var date) ? date : null;
    private static string SelectedDate(DatePicker picker) => picker.SelectedDate?.ToString("yyyy-MM-dd", CultureInfo.InvariantCulture) ?? "";
    private object Config()
    {
        var type = SelectedTag(TriggerBox); var time = SelectedTime(); object trigger = type switch
        {
            "market_relative" => new { type, anchor = "open", offset_minutes = 0 },
            "calendar_periodic" => new { type, months = "*", day = 1, time },
            "once" when OnceDatePicker.SelectedDate is { } date => new { type, date = date.ToString("yyyy-MM-dd", CultureInfo.InvariantCulture), time },
            "once" => throw new InvalidOperationException("请选择单次任务的触发日期"),
            _ => new { type, time },
        };
        return new { name = NameBox.Text, workflow_key = SelectedTag(WorkflowBox), trigger, note = NoteBox.Text, effective_from = SelectedDate(StartDatePicker), effective_until = SelectedDate(EndDatePicker) };
    }
    private void NewTask_Click(object sender, RoutedEventArgs e) { _pendingSave = null; SaveButton.Content = "保存修改"; _selected = null; NameBox.Text = ""; NoteBox.Text = ""; StartDatePicker.SelectedDate = null; EndDatePicker.SelectedDate = null; OnceDatePicker.SelectedDate = null; SelectByTag(WorkflowBox, "companion_judgment"); SelectByTag(TriggerBox, "trading_day_fixed"); SelectTime("09:00"); UpdateTriggerControls(); UpdateRuleText(); PreviewText.Text = "保存前会显示下一次触发预览。"; }
    private void TaskList_SelectionChanged(object sender, SelectionChangedEventArgs e) { if (TaskList.SelectedItem is ScheduleItem item) Apply(item); }
    private async void SaveButton_Click(object sender, RoutedEventArgs e)
    {
        if (_pendingSave is null) { _pendingSave = Config(); await SendAsync("schedule.preview", _pendingSave); return; }
        if (_selected is null) await SendAsync("schedule.create", _pendingSave); else await SendAsync("schedule.update", _pendingSave, _selected.Id, _selected.Version);
    }
    private async void PauseButton_Click(object sender, RoutedEventArgs e) { if (_selected is null) return; await SendAsync(_selected.Status == "active" ? "schedule.pause" : "schedule.resume", null, _selected.Id, _selected.Version); }
    private async void ArchiveButton_Click(object sender, RoutedEventArgs e) { if (_selected is null || _selected.Status == "archived") return; await SendAsync("schedule.archive", null, _selected.Id, _selected.Version); }
    private void TriggerBox_SelectionChanged(object sender, SelectionChangedEventArgs e) { UpdateTriggerControls(); UpdateRuleText(); }
    private void WorkflowBox_SelectionChanged(object sender, SelectionChangedEventArgs e)
    {
        var periodic = SelectedTag(WorkflowBox) == "periodic_review";
        foreach (var item in TriggerBox.Items.OfType<ComboBoxItem>())
            item.IsEnabled = periodic ? (string?)item.Tag is "calendar_periodic" or "once" : (string?)item.Tag is "trading_day_fixed" or "market_relative" or "once";
        if (TriggerBox.SelectedItem is not ComboBoxItem selected || !selected.IsEnabled)
            SelectByTag(TriggerBox, periodic ? "calendar_periodic" : "trading_day_fixed");
        UpdateRuleText();
    }
    private void UpdateTriggerControls()
    {
        var type = SelectedTag(TriggerBox);
        var relative = type == "market_relative";
        TimePickerPanel.Visibility = relative ? Visibility.Collapsed : Visibility.Visible;
        OnceDatePanel.Visibility = type == "once" ? Visibility.Visible : Visibility.Collapsed;
        TriggerHint.Text = relative ? "按开盘时刻触发" : "触发时间";
    }
    private void UpdateRuleText()
    {
        WorkflowValueText.Text = SelectedTag(WorkflowBox) == "periodic_review" ? "当前：定期复盘" : "当前：伴生研判";
        TriggerValueText.Text = SelectedTag(TriggerBox) switch
        {
            "market_relative" => "当前：开收盘相对时间", "calendar_periodic" => "当前：日历周期",
            "once" => "当前：单次任务", _ => "当前：交易日固定时间",
        };
    }
    private void RestoreWindowBounds() { if (WindowStateService.Load(_paths.TaskWindowStatePath) is not { } saved) return; Width = saved.Width; Height = saved.Height; if (saved.Left is not null) Left = saved.Left.Value; if (saved.Top is not null) Top = saved.Top.Value; }
    private void SaveWindowBounds() { var bounds = WindowState == WindowState.Normal ? new Rect(Left, Top, ActualWidth, ActualHeight) : RestoreBounds; WindowStateService.Save(_paths.TaskWindowStatePath, bounds.Width, bounds.Height, bounds.Left, bounds.Top); }

    private sealed record ScheduleItem(string Id, int Version, string Status, string Name, string Workflow, string Trigger, string Time, string TriggerDate, string Note, string EffectiveFrom, string EffectiveUntil, string Session, string Next, DateTimeOffset? NextAt)
    {
        public string Title => Name;
        public string Detail => Trigger switch
        {
            "trading_day_fixed" => $"交易日 · {Time}",
            "market_relative" => "开收盘相对时间",
            "calendar_periodic" => "日历周期",
            "once" => "单次任务",
            _ => "触发规则待确认",
        };
        public static ScheduleItem From(JsonElement item)
        {
            var config = item.GetProperty("config"); var trigger = config.GetProperty("trigger"); var (next, nextAt) = FormatNext(item);
            return new(item.GetProperty("schedule_id").GetString()!, item.GetProperty("version").GetInt32(), item.GetProperty("status").GetString()!, config.GetProperty("name").GetString()!, config.GetProperty("workflow_key").GetString()!, trigger.GetProperty("type").GetString()!, trigger.TryGetProperty("time", out var time) ? time.GetString() ?? "" : "", trigger.TryGetProperty("date", out var date) ? date.GetString() ?? "" : "", config.TryGetProperty("note", out var note) ? note.GetString() ?? "" : "", config.TryGetProperty("effective_from", out var start) && !string.IsNullOrWhiteSpace(start.GetString()) ? start.GetString()! : "立即生效", config.TryGetProperty("effective_until", out var end) && !string.IsNullOrWhiteSpace(end.GetString()) ? end.GetString()! : "长期有效", item.TryGetProperty("session", out var session) ? session.GetString() ?? "" : "", next, nextAt);
        }

        private static (string Text, DateTimeOffset? At) FormatNext(JsonElement item)
        {
            if (!item.TryGetProperty("next_targets", out var targets) || targets.GetArrayLength() == 0)
                return ("下次触发：暂无", null);

            var raw = targets[0].GetString();
            if (DateTimeOffset.TryParse(raw, CultureInfo.InvariantCulture, DateTimeStyles.RoundtripKind, out var next))
                return ($"下次触发：{next.ToOffset(TimeSpan.FromHours(8)):M'月'd'日' HH:mm}", next);
            return ($"下次触发：{raw ?? "暂无"}", null);
        }
    }
}
