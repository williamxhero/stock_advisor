using System.Collections.ObjectModel;
using System.Diagnostics;
using System.Globalization;
using AIDecisionCenter.App.Services;
using AIDecisionCenter.Core.Models;

namespace AIDecisionCenter.App.ViewModels;

public sealed class MainViewModel : ObservableObject
{
    private readonly ITaskMessageStore _store;
    private readonly TaskSyncService _syncService;
    private readonly DesktopNotificationService _notifications;
    private readonly AppPaths _paths;
    private readonly AppSettings _settings;
    private DateTime? _lastCheckAt;
    private DateOnly _tasksDate;
    private TaskRowViewModel? _selectedTask;
    private HistoryRecordViewModel? _selectedHistory;
    private int _selectedSectionIndex;
    private string _statusText = "正在启动…";
    private bool _isBusy;
    private bool _startupEnabled;

    public MainViewModel(
        ITaskMessageStore store,
        TaskSyncService syncService,
        DesktopNotificationService notifications,
        AppPaths paths,
        AppSettings settings)
    {
        _store = store;
        _syncService = syncService;
        _notifications = notifications;
        _paths = paths;
        _settings = settings;
        _startupEnabled = StartupRegistrationService.IsEnabled();
        TodayText = DateTime.Today.ToString("yyyy 年 M 月 d 日 · dddd", CultureInfo.GetCultureInfo("zh-CN"));

        SyncCommand = new AsyncCommand(SyncAsync, () => !IsBusy);
        MarkReadCommand = new AsyncCommand(MarkSelectedReadAsync, () => SelectedMessage is { IsRead: false });
        OpenConfigCommand = new AsyncCommand(OpenConfigFolderAsync);
        ToggleStartupCommand = new AsyncCommand(ToggleStartupAsync);
    }

    public ObservableCollection<TaskRowViewModel> Tasks { get; } = [];

    public ObservableCollection<HistoryRecordViewModel> History { get; } = [];

    public AsyncCommand SyncCommand { get; }

    public AsyncCommand MarkReadCommand { get; }

    public AsyncCommand OpenConfigCommand { get; }

    public AsyncCommand ToggleStartupCommand { get; }

    public TaskRowViewModel? SelectedTask
    {
        get => _selectedTask;
        set
        {
            if (SetProperty(ref _selectedTask, value))
            {
                if (SelectedSectionIndex == 0)
                {
                    NotifySelectionChanged();
                }
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

    public TaskMessage? SelectedMessage => SelectedSectionIndex == 1
        ? SelectedHistory?.Message
        : SelectedTask?.Message;

    public string DetailTitle => SelectedSectionIndex == 1
        ? SelectedHistory?.TaskType ?? "历史消息"
        : SelectedTask?.Name ?? "任务详情";

    public string DetailSlotText => SelectedSectionIndex == 1
        ? SelectedHistory?.Message.Slot.ToString("HH:mm", CultureInfo.InvariantCulture) ?? string.Empty
        : SelectedTask?.TimeText ?? string.Empty;

    public string DetailReceivedAtText => SelectedSectionIndex == 1
        ? SelectedHistory?.ReceivedAtText ?? string.Empty
        : SelectedTask?.ReceivedAtText ?? string.Empty;

    public string DetailCategoryText => SelectedMessage is null
        ? string.Empty
        : $"{SelectedMessage.Project} · {SelectedMessage.TaskType}";

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
                SyncCommand.RaiseCanExecuteChanged();
            }
        }
    }

    public bool IsGmailConfigured => _syncService.IsConfigured;

    public bool IsGmailAuthorized => _syncService.IsAuthorized;

    public string ConnectionText => _syncService.ConfigurationError is not null && File.Exists(_paths.OAuthClientPath)
        ? "OAuth 配置无效"
        : IsGmailConfigured
        ? IsGmailAuthorized ? "Gmail 已连接" : "Gmail 待授权"
        : "Gmail 未配置";

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
        await RefreshAsync().ConfigureAwait(true);
        StatusText = _syncService.ConfigurationError is { } configurationError && File.Exists(_paths.OAuthClientPath)
            ? configurationError
            : IsGmailConfigured
            ? IsGmailAuthorized
                ? "已就绪；节点到达后每 30 秒检查，最多持续 20 分钟"
                : "Gmail OAuth 已配置；点击“立即同步”完成首次浏览器授权"
            : $"未配置 Gmail。把 oauth-client.json 放到 {_paths.DataDirectory}";
    }

    public async Task PollOnceAsync()
    {
        if (!IsGmailAuthorized || IsBusy)
        {
            _lastCheckAt = DateTime.Now;
            return;
        }

        await SyncAsync().ConfigureAwait(true);
    }

    public TimeSpan GetNextPollDelay(DateTime now)
    {
        foreach (var task in Tasks)
        {
            task.RefreshStatus(now);
        }

        var completedSlots = Tasks
            .Where(task => task.IsComplete)
            .Select(task => task.Expected.Slot);
        var retrySeconds = Math.Max(1, _settings.Polling.ActiveSeconds);
        var nodeTimeoutMinutes = Math.Max(1, _settings.Polling.NodeTimeoutMinutes);
        return TaskPollingSchedule.GetDelay(
            now,
            _tasksDate,
            completedSlots,
            _lastCheckAt,
            TimeSpan.FromSeconds(retrySeconds),
            TimeSpan.FromMinutes(nodeTimeoutMinutes));
    }

    private async Task SyncAsync()
    {
        _lastCheckAt = DateTime.Now;
        if (!IsGmailConfigured)
        {
            StatusText = _syncService.ConfigurationError is { } configurationError && File.Exists(_paths.OAuthClientPath)
                ? configurationError
                : "尚未配置 Gmail OAuth；请打开配置目录，放入 oauth-client.json。";
            OpenConfigFolder();
            return;
        }

        IsBusy = true;
        StatusText = "正在检查 Gmail…";
        try
        {
            var added = await _syncService.SyncAsync(_settings).ConfigureAwait(true);
            OnPropertyChanged(nameof(IsGmailAuthorized));
            OnPropertyChanged(nameof(ConnectionText));
            await RefreshAsync().ConfigureAwait(true);
            StatusText = added.Count == 0
                ? $"{DateTime.Now.ToString("HH:mm:ss", CultureInfo.InvariantCulture)} 已同步，没有新任务"
                : $"已保存 {added.Count} 条新消息";
            var today = DateOnly.FromDateTime(DateTime.Today);
            foreach (var task in added.Where(task => task.ScheduledDate == today))
            {
                _notifications.Show(task.TaskType, task.BodyMarkdown.Length > 120 ? task.BodyMarkdown[..120] + "…" : task.BodyMarkdown);
            }
        }
        catch (Exception exception)
        {
            StatusText = $"同步失败：{exception.Message}";
        }
        finally
        {
            IsBusy = false;
        }
    }

    private async Task MarkSelectedReadAsync()
    {
        if (SelectedMessage is not { } message)
        {
            return;
        }

        await _store.MarkReadAsync(message.Id).ConfigureAwait(true);
        await RefreshAsync(message.Slot, message.Id).ConfigureAwait(true);
        StatusText = "已标为已读";
    }

    private Task OpenConfigFolderAsync()
    {
        OpenConfigFolder();
        return Task.CompletedTask;
    }

    private Task ToggleStartupAsync()
    {
        StartupRegistrationService.SetEnabled(!StartupEnabled);
        StartupEnabled = !StartupEnabled;
        StatusText = StartupEnabled ? "已启用开机启动" : "已关闭开机启动";
        return Task.CompletedTask;
    }

    private async Task RefreshAsync(TimeOnly? selectSlot = null, long? selectMessageId = null)
    {
        _tasksDate = DateOnly.FromDateTime(DateTime.Today);
        var messages = await _store.GetForDateAsync(_tasksDate).ConfigureAwait(true);
        var historyMessages = await _store.GetAllAsync().ConfigureAwait(true);
        var latestBySlot = messages
            .GroupBy(message => message.Slot)
            .ToDictionary(group => group.Key, group => group.OrderByDescending(message => message.ReceivedAt).First());

        var slotToSelect = selectSlot ?? SelectedTask?.Expected.Slot;
        var nodeTimeout = TimeSpan.FromMinutes(Math.Max(1, _settings.Polling.NodeTimeoutMinutes));
        Tasks.Clear();
        foreach (var expected in ExpectedTaskCatalog.AShareTasks)
        {
            latestBySlot.TryGetValue(expected.Slot, out var message);
            Tasks.Add(new TaskRowViewModel(expected, message, nodeTimeout));
        }

        OnPropertyChanged(nameof(CompletedCount));
        OnPropertyChanged(nameof(UnreadCount));

        SelectedTask = Tasks.FirstOrDefault(row => row.Expected.Slot == slotToSelect)
            ?? Tasks.FirstOrDefault(row => row.IsUnread)
            ?? Tasks.FirstOrDefault(row => row.IsComplete)
            ?? Tasks.FirstOrDefault();

        var historyIdToSelect = selectMessageId ?? SelectedHistory?.Message.Id;
        History.Clear();
        foreach (var message in historyMessages)
        {
            History.Add(new HistoryRecordViewModel(message));
        }

        OnPropertyChanged(nameof(HistoryCount));
        SelectedHistory = History.FirstOrDefault(row => row.Message.Id == historyIdToSelect)
            ?? History.FirstOrDefault();
    }

    private void NotifySelectionChanged()
    {
        OnPropertyChanged(nameof(SelectedMessage));
        OnPropertyChanged(nameof(DetailTitle));
        OnPropertyChanged(nameof(DetailSlotText));
        OnPropertyChanged(nameof(DetailReceivedAtText));
        OnPropertyChanged(nameof(DetailCategoryText));
        MarkReadCommand.RaiseCanExecuteChanged();
    }

    private void OpenConfigFolder()
    {
        _paths.EnsureDirectories();
        Process.Start(new ProcessStartInfo("explorer.exe", _paths.DataDirectory) { UseShellExecute = true });
    }
}
