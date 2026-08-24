using System.Collections.ObjectModel;
using System.Diagnostics;
using System.Globalization;
using AIDecisionCenter.App.Services;
using AIDecisionCenter.Core.Models;

namespace AIDecisionCenter.App.ViewModels;

public sealed class MainViewModel : ObservableObject
{
    private readonly ITaskMessageStore _store;
    private readonly LocalInboxService _inbox;
    private readonly DesktopNotificationService _notifications;
    private readonly AppPaths _paths;
    private readonly AppSettings _settings;
    private readonly List<TaskMessage> _allHistory = [];
    private TaskRowViewModel? _selectedTask;
    private HistoryRecordViewModel? _selectedHistory;
    private int _selectedSectionIndex;
    private string _statusText = "正在启动…";
    private bool _isBusy;
    private bool _startupEnabled;
    private string _searchText = string.Empty;
    private string _selectedStatusFilter = "全部状态";
    private bool _unreadOnly;
    private bool _starredOnly;
    private bool _includeArchived;
    private string _noteText = string.Empty;

    public MainViewModel(
        ITaskMessageStore store,
        LocalInboxService inbox,
        DesktopNotificationService notifications,
        AppPaths paths,
        AppSettings settings)
    {
        _store = store;
        _inbox = inbox;
        _notifications = notifications;
        _paths = paths;
        _settings = settings;
        _startupEnabled = StartupRegistrationService.IsEnabled();
        TodayText = DateTime.Today.ToString("yyyy 年 M 月 d 日 · dddd", CultureInfo.GetCultureInfo("zh-CN"));

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

    public ObservableCollection<HistoryRecordViewModel> History { get; } = [];

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
    public string DetailCategoryText => SelectedMessage is null ? string.Empty : $"{SelectedMessage.Project} · {StatusTextFor(SelectedMessage.Status)} · {SelectedMessage.Source}";
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

    public string TodayText { get; }
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
        await RefreshAsync().ConfigureAwait(true);
        StatusText = batch.Added.Count > 0 ? $"启动时补收 {batch.Added.Count} 条消息" : "本地 Inbox 已就绪";
        OnPropertyChanged(nameof(ConnectionText));
    }

    public async Task HandleImportBatchAsync(InboxImportBatch batch)
    {
        var latestId = batch.Added.Count > 0 ? batch.Added[batch.Added.Count - 1].Id : (long?)null;
        await RefreshAsync(selectMessageId: latestId).ConfigureAwait(true);
        OnPropertyChanged(nameof(ConnectionText));
        if (batch.Added.Count > 3)
        {
            _notifications.Show("AI Decision Center", $"已补收 {batch.Added.Count} 条定时回复");
        }
        else
        {
            foreach (var message in batch.Added)
            {
                _notifications.Show(message.TaskType, message.Summary);
            }
        }
        StatusText = batch.DeadLetterCount > 0
            ? $"导入 {batch.Added.Count} 条；{batch.DeadLetterCount} 条进入 dead-letter"
            : $"已导入 {batch.Added.Count} 条新消息";
    }

    public void ReportInboxFailure(Exception exception) => StatusText = $"Inbox后台处理失败：{exception.Message}";

    public void RefreshTaskStatuses(DateTime now)
    {
        foreach (var task in Tasks)
        {
            task.RefreshStatus(now);
        }
    }

    private async Task ScanAsync()
    {
        IsBusy = true;
        StatusText = "正在扫描本地 Inbox…";
        try
        {
            var batch = await _inbox.ImportAvailableAsync().ConfigureAwait(true);
            if (batch.Added.Count > 0 || batch.DeadLetterCount > 0)
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
            Filter = "Markdown (*.md)|*.md",
            FileName = $"{message.ScheduledFor:yyyyMMdd-HHmm}_{taskName}_{message.ExternalId[..Math.Min(8, message.ExternalId.Length)]}.md"
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
        var today = DateOnly.FromDateTime(DateTime.Today);
        var todayMessages = await _store.GetForDateAsync(today).ConfigureAwait(true);
        var all = await _store.GetAllAsync().ConfigureAwait(true);
        _allHistory.Clear();
        _allHistory.AddRange(all);

        var previousTaskKey = SelectedTask?.Expected.TaskKey;
        Tasks.Clear();
        foreach (var expected in ExpectedTaskCatalog.AShareTasks)
        {
            var message = todayMessages
                .Where(candidate => string.Equals(candidate.TaskKey, expected.TaskKey, StringComparison.Ordinal) ||
                                    candidate.TaskKey is null && candidate.Slot == expected.Slot)
                .OrderByDescending(candidate => candidate.CompletedAt)
                .FirstOrDefault();
            Tasks.Add(new TaskRowViewModel(
                expected,
                message,
                TimeSpan.FromMinutes(Math.Max(1, _settings.Display.NodeTimeoutMinutes))));
        }
        SelectedTask = Tasks.FirstOrDefault(task => task.Expected.TaskKey == previousTaskKey)
            ?? Tasks.FirstOrDefault(task => task.IsUnread)
            ?? Tasks.FirstOrDefault(task => task.IsComplete)
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
}
