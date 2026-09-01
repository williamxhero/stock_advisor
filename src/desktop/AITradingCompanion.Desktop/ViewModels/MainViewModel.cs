using System.Collections.ObjectModel;
using System.Diagnostics;
using System.Globalization;
using AITradingCompanion.Desktop.Services;
using AITradingCompanion.Core.Models;

namespace AITradingCompanion.Desktop.ViewModels;

public sealed class MainViewModel : ObservableObject
{
    private const string TradingDayUnavailablePrefix = "交易日状态暂不可用：";
    private readonly ITaskMessageStore _store;
    private readonly LocalInboxService _inbox;
    private readonly DesktopNotificationService _notifications;
    private readonly AppPaths _paths;
    private readonly AppSettings _settings;
    private readonly ICompanionGateway _gateway;
    private readonly List<TaskMessage> _allHistory = [];
    private readonly HashSet<string> _dismissedGatewayCycleIds = new(StringComparer.Ordinal);
    private readonly Dictionary<string, string> _todayCycleIdsByTaskKey = new(StringComparer.Ordinal);
    private TaskRowViewModel? _selectedTask;
    private HistoryRecordViewModel? _selectedHistory;
    private int _selectedSectionIndex;
    private string _statusText = string.Empty;
    private bool _isBusy;
    private bool _startupEnabled;
    private string _searchText = string.Empty;
    private string _selectedStatusFilter = "全部状态";
    private bool _unreadOnly;
    private bool _starredOnly;
    private bool _includeArchived;
    private string _noteText = string.Empty;
    private bool _gatewayHistoryAvailable;
    private DateOnly _currentTradingDate;
    private DateOnly? _todayBoardDate;
    private DateOnly? _todayBoardRequestInFlight;
    private bool _isTradingDay;
    private static readonly TimeZoneInfo ShanghaiTimeZone = TimeZoneInfo.FindSystemTimeZoneById("Asia/Shanghai");

    public MainViewModel(
        ITaskMessageStore store,
        LocalInboxService inbox,
        DesktopNotificationService notifications,
        AppPaths paths,
        AppSettings settings,
        ICompanionGateway gateway)
    {
        _store = store;
        _inbox = inbox;
        _notifications = notifications;
        _paths = paths;
        _settings = settings;
        _gateway = gateway;
        _startupEnabled = StartupRegistrationService.IsEnabled();
        _currentTradingDate = ShanghaiDateNow();

        ScanCommand = new AsyncCommand(ScanAsync, () => !IsBusy);
        ToggleReadCommand = new AsyncCommand(ToggleReadAsync, () => SelectedMessage is not null);
        ToggleStarredCommand = new AsyncCommand(ToggleStarredAsync, () => SelectedMessage is not null);
        ToggleArchivedCommand = new AsyncCommand(ToggleArchivedAsync, () => SelectedMessage is not null);
        SaveNoteCommand = new AsyncCommand(SaveNoteAsync, () => SelectedMessage is not null);
        ExportCommand = new AsyncCommand(ExportAsync, () => SelectedMessage is not null);
        OpenConfigCommand = new AsyncCommand(OpenConfigFolderAsync);
        ToggleStartupCommand = new AsyncCommand(ToggleStartupAsync);
    }

    public ObservableCollection<TaskRowViewModel> Tasks { get; } = [];

    public ICompanionGateway Gateway => _gateway;

    public ObservableCollection<HistoryRecordViewModel> History { get; } = [];
    public ObservableCollection<HistoryDateGroupViewModel> HistoryByDate { get; } = [];

    public IReadOnlyList<string> StatusFilters { get; } = ["全部状态", "正常", "已跳过", "失败"];

    public AsyncCommand ScanCommand { get; }
    public AsyncCommand ToggleReadCommand { get; }
    public AsyncCommand ToggleStarredCommand { get; }
    public AsyncCommand ToggleArchivedCommand { get; }
    public AsyncCommand SaveNoteCommand { get; }
    public AsyncCommand ExportCommand { get; }
    public AsyncCommand OpenConfigCommand { get; }
    public AsyncCommand ToggleStartupCommand { get; }

    public TaskRowViewModel? SelectedTask
    {
        get => _selectedTask;
        set
        {
            if (SetProperty(ref _selectedTask, value) && SelectedSectionIndex == 0)
            {
                NotifySelectionChanged();
            }
        }
    }

    public HistoryRecordViewModel? SelectedHistory
    {
        get => _selectedHistory;
        set
        {
            if (SetProperty(ref _selectedHistory, value) && SelectedSectionIndex == 1)
            {
                NotifySelectionChanged();
            }
        }
    }

    public int SelectedSectionIndex
    {
        get => _selectedSectionIndex;
        set
        {
            if (SetProperty(ref _selectedSectionIndex, value))
            {
                NotifySelectionChanged();
            }
        }
    }

    public TaskMessage? SelectedMessage => SelectedSectionIndex == 1 ? SelectedHistory?.Message : SelectedTask?.Message;

    public string SearchText
    {
        get => _searchText;
        set
        {
            if (SetProperty(ref _searchText, value))
            {
                ApplyHistoryFilter();
            }
        }
    }

    public string SelectedStatusFilter
    {
        get => _selectedStatusFilter;
        set
        {
            if (SetProperty(ref _selectedStatusFilter, value))
            {
                ApplyHistoryFilter();
            }
        }
    }

    public bool UnreadOnly
    {
        get => _unreadOnly;
        set
        {
            if (SetProperty(ref _unreadOnly, value))
            {
                ApplyHistoryFilter();
            }
        }
    }

    public bool StarredOnly
    {
        get => _starredOnly;
        set
        {
            if (SetProperty(ref _starredOnly, value))
            {
                ApplyHistoryFilter();
            }
        }
    }

    public bool IncludeArchived
    {
        get => _includeArchived;
        set
        {
            if (SetProperty(ref _includeArchived, value))
            {
                ApplyHistoryFilter();
            }
        }
    }

    public string NoteText
    {
        get => _noteText;
        set => SetProperty(ref _noteText, value);
    }

    public string DetailTitle => SelectedMessage?.TaskType ?? "任务详情";
    public string DetailSlotText => SelectedMessage?.ScheduledFor.ToString("yyyy-MM-dd HH:mm", CultureInfo.InvariantCulture) ?? string.Empty;
    public string DetailReceivedAtText => SelectedMessage?.CompletedAt.ToLocalTime().ToString("yyyy-MM-dd HH:mm:ss", CultureInfo.InvariantCulture) ?? string.Empty;
    public string DetailCategoryText => SelectedMessage is null ? string.Empty : $"{SelectedMessage.Project} · {StatusTextFor(SelectedMessage.Status)} · {SourceTextFor(SelectedMessage.Source)}";
    public string ReadButtonText => SelectedMessage?.IsRead == true ? "标为未读" : "标为已读";
    public string StarButtonText => SelectedMessage?.IsStarred == true ? "取消收藏" : "收藏";
    public string ArchiveButtonText => SelectedMessage?.IsArchived == true ? "恢复归档" : "归档";
    public string LatestReplyText => _allHistory.FirstOrDefault(message => !message.IsArchived) is { } latest
        ? $"最新回复：{latest.TaskType} · {latest.CompletedAt.ToLocalTime():MM-dd HH:mm} · {latest.Summary}"
        : "最新回复：暂无";

    public string StatusText
    {
        get => _statusText;
        private set => SetProperty(ref _statusText, value);
    }

    public bool IsBusy
    {
        get => _isBusy;
        private set
        {
            if (SetProperty(ref _isBusy, value))
            {
                ScanCommand.RaiseCanExecuteChanged();
            }
        }
    }

    public string ConnectionText => _inbox.GetDeadLetterCount() is var count && count > 0
        ? $"本地 Inbox · {count} 条死信"
        : "本地 Inbox 已连接";

    public string TodayText => _currentTradingDate.ToDateTime(TimeOnly.MinValue).ToString("yyyy 年 M 月 d 日 · dddd", CultureInfo.GetCultureInfo("zh-CN"));
    public DateOnly CurrentTradingDate => _currentTradingDate;
    public int CompletedCount => Tasks.Count(task => task.IsComplete);
    public int UnreadCount => Tasks.Count(task => task.IsUnread);
    public int HistoryCount => History.Count;

    public bool StartupEnabled
    {
        get => _startupEnabled;
        private set
        {
            if (SetProperty(ref _startupEnabled, value))
            {
                OnPropertyChanged(nameof(StartupStateText));
            }
        }
    }

    public string StartupStateText => StartupEnabled ? "已开启" : "已关闭";

    public async Task InitializeAsync()
    {
        await _store.InitializeAsync().ConfigureAwait(true);
        var batch = await _inbox.ImportAvailableAsync().ConfigureAwait(true);
        await RefreshGatewayHistoryAsync().ConfigureAwait(true);
        await RefreshTodayBoardAsync().ConfigureAwait(true);
        StatusText = batch.ReconciliationErrorCount > 0
            ? $"归档对账失败 {batch.ReconciliationErrorCount} 条"
            : batch.RecoveredCount > 0
                ? $"启动时从归档补收 {batch.RecoveredCount} 条消息"
                : batch.Added.Count > 0 ? $"启动时补收 {batch.Added.Count} 条消息" : string.Empty;
        OnPropertyChanged(nameof(ConnectionText));
    }

    private async Task RefreshGatewayHistoryAsync()
    {
        for (var attempt = 0; attempt < 12; attempt++)
        {
            try
            {
                var page = await _gateway.GetSnapshotAsync("history", new Dictionary<string, string> { ["limit"] = "90" }).ConfigureAwait(true);
                if (page["items"] is not System.Text.Json.Nodes.JsonArray items) return;
                foreach (var node in items.OfType<System.Text.Json.Nodes.JsonObject>())
                {
                    var cycleId = node["cycle_id"]?.GetValue<string>();
                    var scheduled = node["scheduled_for"]?.GetValue<string>();
                    var taskKey = node["task_key"]?.GetValue<string>();
                    if (string.IsNullOrWhiteSpace(cycleId) || string.IsNullOrWhiteSpace(scheduled) || string.IsNullOrWhiteSpace(taskKey)) continue;
                    var at = DateTimeOffset.Parse(scheduled, CultureInfo.InvariantCulture);
                    var expected = ExpectedTaskCatalog.AShareTasks.FirstOrDefault(item => item.TaskKey == taskKey);
                    var displayName = expected?.Name ?? taskKey;
                    if (node["task_profile_json"]?.GetValue<string>() is { Length: > 0 } profileJson)
                    {
                        try { displayName = System.Text.Json.Nodes.JsonNode.Parse(profileJson)?["display_name"]?.GetValue<string>() ?? displayName; }
                        catch (System.Text.Json.JsonException) { }
                    }
                    var state = node["state"]?.GetValue<string>() ?? "queued";
                    var status = state is "failed" or "waiting_for_repair" ? TaskMessageStatus.Failed : TaskMessageStatus.Succeeded;
                    await _store.AddAsync(new IncomingTaskMessage($"gateway:{cycleId}", "gateway", cycleId, "AI Trading Companion", taskKey,
                        displayName, at, at, at, status, string.Empty, string.Empty,
                        $"运行时状态：{state}", $"# {displayName}\n\n运行时状态：{state}", node.ToJsonString(), cycleId)).ConfigureAwait(true);
                }
                _gatewayHistoryAvailable = true;
                return;
            }
            catch (Exception) when (attempt < 11)
            {
                await Task.Delay(250).ConfigureAwait(true);
            }
            catch (Exception exception)
            {
                StatusText = $"运行时历史暂不可用：{exception.Message}";
            }
        }
    }

    public async Task HandleImportBatchAsync(InboxImportBatch batch)
    {
        var latestId = batch.Added.Count > 0 ? batch.Added[batch.Added.Count - 1].Id : (long?)null;
        await RefreshAsync(selectMessageId: latestId).ConfigureAwait(true);
        OnPropertyChanged(nameof(ConnectionText));
        if (batch.Added.Count > 3)
        {
            _notifications.Show("AI交易伙伴", $"已补收 {batch.Added.Count} 条历史消息");
        }
        else
        {
            foreach (var message in batch.Added)
            {
                _notifications.Show(message.TaskType, message.Summary);
            }
        }
        StatusText = batch.ReconciliationErrorCount > 0
            ? $"导入 {batch.Added.Count} 条；归档对账失败 {batch.ReconciliationErrorCount} 条"
            : batch.DeadLetterCount > 0
            ? $"导入 {batch.Added.Count} 条；{batch.DeadLetterCount} 条进入 dead-letter"
            : batch.RecoveredCount > 0
                ? $"已从归档补收 {batch.RecoveredCount} 条消息"
                : $"已导入 {batch.Added.Count} 条新消息";
    }

    public void ReportInboxFailure(Exception exception) => StatusText = $"Inbox后台处理失败：{exception.Message}";

    public void RefreshTaskStatuses(DateTime now)
    {
        foreach (var task in Tasks)
        {
            task.RefreshStatus(now);
        }
        OnPropertyChanged(nameof(CompletedCount));
    }

    public void RefreshCompanionSummary() => OnPropertyChanged(nameof(CompletedCount));

    public void SynchronizeManualCycles(IEnumerable<CompanionWorkspaceProjection> projections)
    {
        var timeout = TimeSpan.FromMinutes(Math.Max(1, _settings.Display.NodeTimeoutMinutes));
        var current = projections.Where(item => item.Trigger == "manual_chat" && item.RequestedAt is not null && IsCurrentTradingDate(item.RequestedAt!.Value)).ToArray();
        var dismissed = current.Where(item => item.IsDismissed).ToArray();
        foreach (var projection in dismissed)
        {
            var existing = Tasks.FirstOrDefault(task => task.CycleId == projection.CycleId);
            if (existing is not null) Tasks.Remove(existing);
        }
        var newlyDismissed = dismissed
            .Select(item => item.CycleId)
            .Where(cycleId => _dismissedGatewayCycleIds.Add(cycleId))
            .ToArray();
        if (newlyDismissed.Length > 0) _ = RemoveDismissedGatewayCyclesAsync(newlyDismissed);
        foreach (var projection in current.Where(item => !item.IsDismissed))
        {
            var existing = Tasks.FirstOrDefault(task => task.CycleId == projection.CycleId);
            if (existing is not null)
            {
                existing.UpdateCompanionStatus(projection.State, projection.ErrorText);
                continue;
            }
            var requested = TimeZoneInfo.ConvertTime(projection.RequestedAt!.Value, ShanghaiTimeZone);
            var expected = new ExpectedTask(
                projection.TaskKey ?? "manual.analysis",
                TimeOnly.FromDateTime(requested.DateTime),
                projection.TaskProfileDisplayName ?? projection.TaskProfileId ?? "手动研判");
            var row = new TaskRowViewModel(expected, null, timeout, projection.CycleId);
            row.UpdateCompanionStatus(projection.State, projection.ErrorText);
            Tasks.Add(row);
        }
    }

    private async Task RemoveDismissedGatewayCyclesAsync(IReadOnlyCollection<string> cycleIds)
    {
        try
        {
            if (await _store.RemoveGatewayCyclesAsync(cycleIds).ConfigureAwait(true) > 0)
            {
                await RefreshAsync().ConfigureAwait(true);
            }
        }
        catch (Exception exception)
        {
            StatusText = $"清理已隐藏研判的本地缓存失败：{exception.Message}";
        }
    }

    public async Task RefreshTodayBoardAsync()
    {
        var today = ShanghaiDateNow();
        if (_currentTradingDate != today)
        {
            _currentTradingDate = today;
            OnPropertyChanged(nameof(CurrentTradingDate));
            OnPropertyChanged(nameof(TodayText));
        }
        if (_todayBoardDate == today) return;
        if (_todayBoardRequestInFlight == today) return;
        _todayBoardRequestInFlight = today;

        try
        {
            var snapshot = await _gateway.GetSnapshotAsync("today", new Dictionary<string, string>
            {
                ["date"] = today.ToString("yyyy-MM-dd", CultureInfo.InvariantCulture),
            }).ConfigureAwait(true);
            _isTradingDay = snapshot["is_trading_day"]?.GetValue<bool>() ?? false;
            _todayCycleIdsByTaskKey.Clear();
            if (snapshot["projections"] is System.Text.Json.Nodes.JsonArray projections)
            {
                foreach (var node in projections.OfType<System.Text.Json.Nodes.JsonObject>())
                {
                    var cycle = node["cycle"] as System.Text.Json.Nodes.JsonObject;
                    var taskKey = cycle?["task_key"]?.GetValue<string>();
                    var cycleId = cycle?["cycle_id"]?.GetValue<string>();
                    if (!string.IsNullOrWhiteSpace(taskKey) && !string.IsNullOrWhiteSpace(cycleId))
                        _todayCycleIdsByTaskKey[taskKey] = cycleId;
                }
            }
            _todayBoardDate = today;
            StatusText = TradingDayStatusAfterSuccess(StatusText);
        }
        catch (Exception exception)
        {
            _isTradingDay = false;
            _todayBoardDate = null;
            StatusText = $"{TradingDayUnavailablePrefix}{exception.Message}";
        }
        finally
        {
            _todayBoardRequestInFlight = null;
        }

        await RefreshAsync().ConfigureAwait(true);
    }

    internal static string TradingDayStatusAfterSuccess(string currentStatus) =>
        currentStatus.StartsWith(TradingDayUnavailablePrefix, StringComparison.Ordinal)
            ? string.Empty
            : currentStatus;

    public bool IsCurrentTradingDate(DateTimeOffset value) =>
        DateOnly.FromDateTime(TimeZoneInfo.ConvertTime(value, ShanghaiTimeZone).DateTime) == _currentTradingDate;

    private async Task ScanAsync()
    {
        IsBusy = true;
        StatusText = "正在扫描本地 Inbox…";
        try
        {
            var batch = await _inbox.ImportAvailableAsync().ConfigureAwait(true);
            if (batch.Added.Count > 0 || batch.DeadLetterCount > 0 || batch.ReconciliationErrorCount > 0)
            {
                await HandleImportBatchAsync(batch).ConfigureAwait(true);
            }
            else
            {
                StatusText = $"{DateTime.Now:HH:mm:ss} 已扫描，没有新消息";
            }
        }
        catch (Exception exception)
        {
            StatusText = $"扫描失败：{exception.Message}";
        }
        finally
        {
            IsBusy = false;
        }
    }

    private async Task ToggleReadAsync()
    {
        if (SelectedMessage is not { } message) return;
        await _store.SetReadAsync(message.Id, !message.IsRead).ConfigureAwait(true);
        await RefreshAsync(selectMessageId: message.Id).ConfigureAwait(true);
    }

    private async Task ToggleStarredAsync()
    {
        if (SelectedMessage is not { } message) return;
        await _store.SetStarredAsync(message.Id, !message.IsStarred).ConfigureAwait(true);
        await RefreshAsync(selectMessageId: message.Id).ConfigureAwait(true);
    }

    private async Task ToggleArchivedAsync()
    {
        if (SelectedMessage is not { } message) return;
        await _store.SetArchivedAsync(message.Id, !message.IsArchived).ConfigureAwait(true);
        await RefreshAsync(selectMessageId: message.Id).ConfigureAwait(true);
        StatusText = message.IsArchived ? "已恢复归档" : "已归档";
    }

    private async Task SaveNoteAsync()
    {
        if (SelectedMessage is not { } message) return;
        await _store.SaveNoteAsync(message.Id, NoteText).ConfigureAwait(true);
        await RefreshAsync(selectMessageId: message.Id).ConfigureAwait(true);
        StatusText = "备注已保存";
    }

    private Task ExportAsync()
    {
        if (SelectedMessage is not { } message) return Task.CompletedTask;
        var invalid = Path.GetInvalidFileNameChars();
        var taskName = new string(message.TaskType.Select(character => invalid.Contains(character) ? '_' : character).ToArray());
        var dialog = new Microsoft.Win32.SaveFileDialog
        {
            Filter = "Text (*.txt)|*.txt",
            FileName = $"{message.ScheduledFor:yyyyMMdd-HHmm}_{taskName}_{message.ExternalId[..Math.Min(8, message.ExternalId.Length)]}.txt"
        };
        if (dialog.ShowDialog() == true)
        {
            File.WriteAllText(dialog.FileName, message.BodyMarkdown, System.Text.Encoding.UTF8);
            StatusText = $"已导出：{dialog.FileName}";
        }
        return Task.CompletedTask;
    }

    private Task OpenConfigFolderAsync()
    {
        _paths.EnsureDirectories();
        Process.Start(new ProcessStartInfo("explorer.exe", _paths.DataDirectory) { UseShellExecute = true });
        return Task.CompletedTask;
    }

    private Task ToggleStartupAsync()
    {
        StartupRegistrationService.SetEnabled(!StartupEnabled);
        StartupEnabled = !StartupEnabled;
        StatusText = StartupEnabled ? "已启用开机启动" : "已关闭开机启动";
        return Task.CompletedTask;
    }

    private async Task RefreshAsync(long? selectMessageId = null)
    {
        var today = _currentTradingDate;
        var todayMessages = await _store.GetForDateAsync(today).ConfigureAwait(true);
        var all = await _store.GetAllAsync().ConfigureAwait(true);
        _allHistory.Clear();
        _allHistory.AddRange(all);

        var previousTaskKey = SelectedTask?.Expected.TaskKey;
        Tasks.Clear();
        foreach (var expected in ExpectedTaskCatalog.ForTradingDay(_isTradingDay))
        {
            var message = todayMessages
                .Where(candidate => string.Equals(candidate.TaskKey, expected.TaskKey, StringComparison.Ordinal) ||
                                    candidate.TaskKey is null && candidate.Slot == expected.Slot)
                .OrderByDescending(candidate => candidate.CompletedAt)
                .FirstOrDefault();
            Tasks.Add(new TaskRowViewModel(
                expected,
                message,
                TimeSpan.FromMinutes(Math.Max(1, _settings.Display.NodeTimeoutMinutes)),
                _todayCycleIdsByTaskKey.GetValueOrDefault(expected.TaskKey)));
        }
        var currentSlot = TimeOnly.FromDateTime(DateTime.Now);
        SelectedTask = Tasks.FirstOrDefault(task => task.Expected.TaskKey == previousTaskKey)
            ?? Tasks.LastOrDefault(task => task.Expected.Slot <= currentSlot)
            ?? Tasks.FirstOrDefault();
        ApplyHistoryFilter(selectMessageId);
        OnPropertyChanged(nameof(CompletedCount));
        OnPropertyChanged(nameof(UnreadCount));
        OnPropertyChanged(nameof(LatestReplyText));
    }

    private void ApplyHistoryFilter(long? selectMessageId = null)
    {
        var previousId = selectMessageId ?? SelectedHistory?.Message.Id;
        IEnumerable<TaskMessage> query = _allHistory;
        if (_gatewayHistoryAvailable) query = query.Where(message => message.Source == "gateway");
        if (!IncludeArchived) query = query.Where(message => !message.IsArchived);
        if (UnreadOnly) query = query.Where(message => !message.IsRead);
        if (StarredOnly) query = query.Where(message => message.IsStarred);
        query = SelectedStatusFilter switch
        {
            "正常" => query.Where(message => message.Status == TaskMessageStatus.Succeeded),
            "已跳过" => query.Where(message => message.Status == TaskMessageStatus.Skipped),
            "失败" => query.Where(message => message.Status == TaskMessageStatus.Failed),
            _ => query
        };
        if (!string.IsNullOrWhiteSpace(SearchText))
        {
            query = query.Where(message =>
                message.TaskType.Contains(SearchText, StringComparison.OrdinalIgnoreCase) ||
                message.Summary.Contains(SearchText, StringComparison.OrdinalIgnoreCase) ||
                message.BodyMarkdown.Contains(SearchText, StringComparison.OrdinalIgnoreCase) ||
                message.Note.Contains(SearchText, StringComparison.OrdinalIgnoreCase) ||
                (message.TaskKey?.Contains(SearchText, StringComparison.OrdinalIgnoreCase) ?? false));
        }

        History.Clear();
        foreach (var message in query)
        {
            History.Add(new HistoryRecordViewModel(message));
        }
        HistoryByDate.Clear();
        foreach (var group in History.GroupBy(item => DateOnly.FromDateTime(item.Message.ReceivedAt.LocalDateTime)).OrderByDescending(group => group.Key))
        {
            HistoryByDate.Add(new HistoryDateGroupViewModel(group.Key, group));
        }
        SelectedHistory = History.FirstOrDefault(item => item.Message.Id == previousId) ?? History.FirstOrDefault();
        OnPropertyChanged(nameof(HistoryCount));
    }

    private void NotifySelectionChanged()
    {
        NoteText = SelectedMessage?.Note ?? string.Empty;
        OnPropertyChanged(nameof(SelectedMessage));
        OnPropertyChanged(nameof(DetailTitle));
        OnPropertyChanged(nameof(DetailSlotText));
        OnPropertyChanged(nameof(DetailReceivedAtText));
        OnPropertyChanged(nameof(DetailCategoryText));
        OnPropertyChanged(nameof(ReadButtonText));
        OnPropertyChanged(nameof(StarButtonText));
        OnPropertyChanged(nameof(ArchiveButtonText));
        ToggleReadCommand.RaiseCanExecuteChanged();
        ToggleStarredCommand.RaiseCanExecuteChanged();
        ToggleArchivedCommand.RaiseCanExecuteChanged();
        SaveNoteCommand.RaiseCanExecuteChanged();
        ExportCommand.RaiseCanExecuteChanged();
    }

    private static string StatusTextFor(TaskMessageStatus status) => status switch
    {
        TaskMessageStatus.Succeeded => "正常",
        TaskMessageStatus.Skipped => "已跳过",
        TaskMessageStatus.Failed => "失败",
        _ => string.Empty
    };

    private static string SourceTextFor(string source) => source switch
    {
        "stock_advisor" => "本地研判服务",
        "gmail-legacy" => "历史导入",
        _ => "本地记录",
    };

    private static DateOnly ShanghaiDateNow() =>
        DateOnly.FromDateTime(TimeZoneInfo.ConvertTime(DateTimeOffset.UtcNow, ShanghaiTimeZone).DateTime);
}
