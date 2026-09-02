using System.ComponentModel;
using System.Diagnostics;
using System.Globalization;
using System.Text.Json;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Input;
using System.Windows.Media;
using System.Windows.Media.Animation;
using System.Windows.Threading;
using AITradingCompanion.Desktop.Converters;
using AITradingCompanion.Desktop.Services;
using AITradingCompanion.Desktop.ViewModels;
using Brush = System.Windows.Media.Brush;
using Brushes = System.Windows.Media.Brushes;
using Button = System.Windows.Controls.Button;
using Color = System.Windows.Media.Color;
using Cursors = System.Windows.Input.Cursors;
using HorizontalAlignment = System.Windows.HorizontalAlignment;
using MessageBox = System.Windows.MessageBox;
using Point = System.Windows.Point;

namespace AITradingCompanion.Desktop.Views;

public partial class MainWindow : Window, IDisposable
{
    private readonly MainViewModel _viewModel;
    private readonly AppPaths _paths;
    private readonly DispatcherTimer _saveSizeTimer;
    private readonly TextToSpeechService _speech;
    private readonly CompanionExchangeService _companionExchange;
    private readonly VoiceRecordingService _companionRecorder = new();
    private readonly DispatcherTimer _companionTimer;
    private readonly Dictionary<string, List<CompanionTimelineEntry>> _localStagedByCycle = new(StringComparer.Ordinal);
    private readonly Dictionary<string, string> _judgmentDrafts;
    private readonly HashSet<string> _locallyWithdrawnMessageIds = new(StringComparer.Ordinal);
    private readonly HashSet<string> _locallyLockedCycles = new(StringComparer.Ordinal);
    private readonly HashSet<string> _editGraceRequestedCycles = new(StringComparer.Ordinal);
    private readonly Dictionary<string, FrameworkElement> _aiAnchors = new(StringComparer.OrdinalIgnoreCase);
    private readonly Dictionary<string, CompanionAiTimelineEntry> _localAiNoticesByCycle = new(StringComparer.Ordinal);
    private readonly Queue<double> _waveformLevels = new();
    private CompanionWorkspaceProjection? _companionProjection;
    private PortfolioWorkspaceProjection? _portfolioProjection;
    private PortfolioWindow? _portfolioWindow;
    private TaskManagementWindow? _taskManagementWindow;
    private EvaluationObservatoryWindow? _evaluationObservatoryWindow;
    private string? _activeAiMarkdown;
    private string? _requestedProjectionCycleId;
    private string? _editingStagedMessageId;
    private DateTimeOffset _nextRuntimeHealthCheck = DateTimeOffset.MinValue;
    private VoiceInputState _voiceState;
    private string? _displayedDraftCycleId;
    private string? _voiceOriginCycleId;
    private string? _voiceContextFile;
    private bool _suppressDraftUpdate;
    private bool _disposed;
    private string? _pendingMemoryExportCommandId;
    private readonly HashSet<string> _handledMemoryResults = new(StringComparer.Ordinal);

    public MainWindow(MainViewModel viewModel, AppPaths paths)
    {
        InitializeComponent();
        _viewModel = viewModel;
        _paths = paths;
        _judgmentDrafts = CompanionDraftStore.Load(paths);
        _speech = new TextToSpeechService(paths);
        _companionExchange = new CompanionExchangeService(paths);
        _saveSizeTimer = new DispatcherTimer { Interval = TimeSpan.FromMilliseconds(400) };
        _saveSizeTimer.Tick += OnSaveSizeTimerTick;
        _companionTimer = new DispatcherTimer { Interval = TimeSpan.FromSeconds(1) };
        _companionTimer.Tick += (_, _) => RefreshCompanionWorkspace();
        _companionTimer.Start();
        _speech.StateChanged += OnSpeechStateChanged;
        _companionRecorder.LevelChanged += OnVoiceLevelChanged;
        DataContext = viewModel;
        RestoreSavedSize();
        viewModel.PropertyChanged += OnViewModelPropertyChanged;
        SizeChanged += OnSizeChanged;
        LocationChanged += OnLocationChanged;
        Closed += OnClosed;
        RefreshCompanionWorkspace();
        UpdateInputState();
    }

    private void OnViewModelPropertyChanged(object? sender, PropertyChangedEventArgs e)
    {
        if (e.PropertyName != nameof(MainViewModel.SelectedMessage)) return;
        _speech.Stop();
        RefreshCompanionWorkspace();
    }

    private void OnClosed(object? sender, EventArgs e)
    {
        SaveCurrentSize();
        CompanionDraftStore.Save(_paths, _judgmentDrafts);
        Dispose();
    }

    public void Dispose()
    {
        if (_disposed) return;
        _disposed = true;
        _saveSizeTimer.Stop();
        _saveSizeTimer.Tick -= OnSaveSizeTimerTick;
        _companionTimer.Stop();
        _speech.StateChanged -= OnSpeechStateChanged;
        _companionRecorder.LevelChanged -= OnVoiceLevelChanged;
        _speech.Dispose();
        _companionRecorder.Dispose();
        _portfolioWindow?.Close();
        SizeChanged -= OnSizeChanged;
        LocationChanged -= OnLocationChanged;
        _viewModel.PropertyChanged -= OnViewModelPropertyChanged;
        Closed -= OnClosed;
        GC.SuppressFinalize(this);
    }

    private async void ReadAloudButton_Click(object sender, RoutedEventArgs e)
    {
        if (_speech.IsSpeaking)
        {
            _speech.Stop();
            return;
        }
        var text = SpeechTextPreprocessor.Prepare(MarkdownDocumentBuilder.ToPlainText(_activeAiMarkdown));
        if (string.IsNullOrWhiteSpace(text)) return;
        try { await _speech.SpeakAsync(text); }
        catch (Exception exception)
        {
            MessageBox.Show(this, $"无法启动朗读：{exception.Message}", "AI交易伙伴", MessageBoxButton.OK, MessageBoxImage.Warning);
        }
    }

    private async void MainSend_Click(object sender, RoutedEventArgs e)
    {
        if (_companionProjection is null) return;
        var text = MainJudgmentInputBox.Text.Trim();
        if (string.IsNullOrWhiteSpace(text)) return;
        var messageId = _editingStagedMessageId ?? Guid.NewGuid().ToString();
        var phase = CompanionInputPolicy.MessagePhase(_companionProjection.State, IsH0LockedForUi());
        var editing = _editingStagedMessageId is not null;
        if (!await SendCompanionCommandAsync(editing ? "edit_staged_message" : "stage_message", text, messageId: messageId)) return;
        if (editing)
        {
            var index = LocalStaged().FindIndex(message => message.MessageId == messageId);
            if (index >= 0) LocalStaged()[index] = LocalStaged()[index] with { Text = text };
        }
        else LocalStaged().Add(new CompanionTimelineEntry(
            DateTimeOffset.Now, text, false, null, messageId, "staged", phase));
        _editingStagedMessageId = null;
        MainJudgmentInputBox.Clear();
        if (_displayedDraftCycleId is not null)
        {
            _judgmentDrafts[_displayedDraftCycleId] = string.Empty;
            CompanionDraftStore.Save(_paths, _judgmentDrafts);
        }
        RenderUserMessages();
        UpdateInputState();
    }

    private async void MainCommit_Click(object sender, RoutedEventArgs e)
    {
        if (_companionProjection is null) return;
        var isConversation = _companionProjection.State == "open";
        var isPreM0 = _companionProjection.State == "queued";
        var isH0 = !isPreM0 && !IsH0LockedForUi();
        if (isConversation) isH0 = false;
        var staged = CombinedUserMessages().Count(message => message.State == "staged");
        var h0Locked = IsH0LockedForUi();
        if (!CompanionInputPolicy.CanCommit(_companionProjection.State, h0Locked, staged)) return;
        var type = isConversation ? "commit_conversation_batch" : isPreM0 ? "commit_pre_m0" : isH0 ? "commit_h0" : "commit_chat_batch";
        if (!isH0 && staged == 0) return;
        SetLocalAiNotice(_companionProjection.CycleId, "chat_pending", "正在想…");
        if (!await SendCompanionCommandAsync(type, null, showAiError: true)) return;
        if (isH0) _locallyLockedCycles.Add(_companionProjection.CycleId);
        UpdateInputState();
    }

    private async void WithdrawStaged_Click(object sender, RoutedEventArgs e)
    {
        if (sender is not Button { Tag: string messageId } || _companionProjection is null) return;
        if (!await SendCompanionCommandAsync("withdraw_staged_message", null, messageId: messageId)) return;
        _locallyWithdrawnMessageIds.Add(messageId);
        LocalStaged().RemoveAll(message => message.MessageId == messageId);
        RenderUserMessages();
        UpdateInputState();
    }

    private void EditStaged_Click(object sender, RoutedEventArgs e)
    {
        if (sender is not Button { Tag: CompanionTimelineEntry entry } || entry.MessageId is null) return;
        _editingStagedMessageId = entry.MessageId;
        MainJudgmentInputBox.Text = entry.Text;
        MainJudgmentInputBox.Focus();
        MainJudgmentInputBox.CaretIndex = MainJudgmentInputBox.Text.Length;
    }

    private async void MainVoice_Click(object sender, RoutedEventArgs e)
    {
        if (_voiceState is VoiceInputState.Idle or VoiceInputState.Error)
        {
            if (_companionProjection?.State != "queued"
                && !IsH0LockedForUi()
                && !await SendCompanionCommandAsync("begin_voice_capture", null)) return;
            try
            {
                var audio = Path.Combine(_paths.CompanionAudioDirectory, $"{DateTimeOffset.UtcNow:yyyyMMdd-HHmmss}-{Guid.NewGuid():N}.wav");
                _voiceOriginCycleId = _companionProjection?.CycleId;
                _voiceContextFile = Path.ChangeExtension(audio, ".context.txt");
                await File.WriteAllTextAsync(_voiceContextFile, BuildAsrContext());
                _companionRecorder.Start(audio);
                SetVoiceState(VoiceInputState.Recording);
            }
            catch (Exception exception)
            {
                SetVoiceState(VoiceInputState.Error);
                _viewModel.ReportInboxFailure(exception);
            }
            return;
        }
        if (_voiceState != VoiceInputState.Recording) return;
        try
        {
            var audio = _companionRecorder.StopAndSave();
            SetVoiceState(VoiceInputState.Transcribing);
            AppendToDraft(_voiceOriginCycleId, await TranscribeAsync(audio, _voiceContextFile));
            SetVoiceState(VoiceInputState.Idle);
        }
        catch (Exception exception)
        {
            SetVoiceState(VoiceInputState.Error);
            _viewModel.ReportInboxFailure(exception);
        }
    }

    private async Task<bool> SendCompanionCommandAsync(
        string type,
        string? text,
        string? messageId = null,
        bool showAiError = false)
    {
        if (_companionProjection is null)
        {
            _viewModel.ReportInboxFailure(new InvalidOperationException("当前判断尚未收到 AI 研究。"));
            return false;
        }
        try
        {
            await _companionExchange.SendAsync(new
            {
                contract = "companion-user-command/v1",
                command_id = Guid.NewGuid().ToString(),
                cycle_id = _companionProjection.CycleId,
                type,
                text,
                message_id = messageId,
            });
            return true;
        }
        catch (Exception exception)
        {
            _viewModel.ReportInboxFailure(exception);
            if (showAiError)
                SetLocalAiNotice(_companionProjection.CycleId, "fault", $"提交失败：{exception.Message}");
            return false;
        }
    }

    private void RefreshCompanionWorkspace()
    {
        if (DateTimeOffset.UtcNow >= _nextRuntimeHealthCheck)
        {
            _nextRuntimeHealthCheck = DateTimeOffset.UtcNow.AddSeconds(30);
            try { CompanionRuntimeService.EnsureStarted(); }
            catch (Exception exception) { _viewModel.ReportInboxFailure(exception); }
        }
        var events = _companionExchange.ReadLatestEvents(1000);
        HandleMemoryResults(events);
        _taskManagementWindow?.UpdateEvents(events);
        RequestTodayProjectionsAsync();
        _viewModel.SynchronizeManualCycles(CompanionEventProjection.ProjectAll(events));
        _portfolioProjection = PortfolioEventProjection.Project(events);
        _portfolioWindow?.UpdateProjection(_portfolioProjection);
        if (_viewModel.SelectedSectionIndex != 0)
        {
            var historicalCycleId = _viewModel.SelectedMessage is { Source: "gateway", SourceRunId.Length: > 0 } historical
                ? historical.SourceRunId
                : null;
            _companionProjection = historicalCycleId is null
                ? null
                : CompanionEventProjection.ProjectForCycle(events, historicalCycleId);
            SwitchDraft(null);
            if (_companionProjection is { } historicalProjection)
            {
                ResolveLocalAiNotice(historicalProjection.CycleId, historicalProjection.AiMessages);
                RenderAiMessages(WithLocalAiNotice(historicalProjection.CycleId, historicalProjection.AiMessages));
            }
            else
            {
                RenderHistoricalMessage();
            }
            RenderUserMessages();
            UpdateInputState();
            if (historicalCycleId is not null) RequestCompanionProjectionAsync(historicalCycleId);
            return;
        }

        foreach (var task in _viewModel.Tasks)
        {
            var taskProjection = task.CycleId is { } cycleId
                ? CompanionEventProjection.ProjectForCycle(events, cycleId)
                : CompanionEventProjection.ProjectForTask(events, task.Expected.TaskKey);
            if (taskProjection?.ScheduledFor is { } scheduled && !_viewModel.IsCurrentTradingDate(scheduled)) taskProjection = null;
            task.UpdateCompanionStatus(taskProjection?.State, taskProjection?.ErrorText);
        }
        _viewModel.RefreshCompanionSummary();
        var selected = _viewModel.SelectedTask;
        var taskKey = selected?.Expected.TaskKey;
        var projection = selected?.CycleId is { } selectedCycleId
            ? CompanionEventProjection.ProjectForCycle(events, selectedCycleId)
            : CompanionEventProjection.ProjectForTask(events, taskKey);
        if (projection?.ScheduledFor is { } projectionScheduled && !_viewModel.IsCurrentTradingDate(projectionScheduled)) projection = null;
        _companionProjection = projection;
        SwitchDraft(taskKey == "conversation.daily" && projection is not null ? CompanionDraftStore.ConversationDraftKey : projection?.CycleId);
        if (projection is null)
        {
            RenderAiMessages([]);
            RenderUserMessages();
            UpdateInputState();
            return;
        }

        if (projection.IsH0Locked) _locallyLockedCycles.Remove(projection.CycleId);
        var knownIds = projection.UserMessages.Select(message => message.MessageId).Where(id => id is not null).ToHashSet(StringComparer.Ordinal);
        LocalStaged().RemoveAll(message => message.MessageId is not null && knownIds.Contains(message.MessageId));
        ResolveLocalAiNotice(projection.CycleId, projection.AiMessages);
        RenderAiMessages(WithLocalAiNotice(projection.CycleId, projection.AiMessages));
        RenderUserMessages();
        UpdateInputState();
        RequestCompanionProjectionAsync(projection.CycleId);
    }

    private async void MemoryButton_Click(object sender, RoutedEventArgs e)
    {
        if (_pendingMemoryExportCommandId is not null) return;
        var commandId = Guid.NewGuid().ToString();
        _pendingMemoryExportCommandId = commandId;
        try
        {
            await _companionExchange.SendAsync(new
            {
                contract = "memory-user-command/v1", command_id = commandId, type = "memory.export",
            });
            MessageBox.Show(this, "正在导出全部私人记忆。导出成功后会再次询问是否清空；此操作不会修改 MarketHub 或 8815 的公共历史。", "AI交易伙伴", MessageBoxButton.OK, MessageBoxImage.Information);
        }
        catch (Exception exception)
        {
            _pendingMemoryExportCommandId = null;
            _viewModel.ReportInboxFailure(exception);
        }
    }

    private async void HandleMemoryResults(IReadOnlyList<string> events)
    {
        foreach (var raw in events)
        {
            using var document = JsonDocument.Parse(raw);
            var root = document.RootElement;
            if (!root.TryGetProperty("contract", out var contract) || contract.GetString() != "memory-command-result/v1") continue;
            var commandId = root.GetProperty("command_id").GetString();
            if (string.IsNullOrWhiteSpace(commandId) || !_handledMemoryResults.Add(commandId)) continue;
            var result = root.GetProperty("result");
            var state = result.TryGetProperty("state", out var stateValue) ? stateValue.GetString() : null;
            if (state == "exported" && commandId == _pendingMemoryExportCommandId)
            {
                _pendingMemoryExportCommandId = null;
                var exportPath = result.GetProperty("machine_export_path").GetString();
                var token = result.GetProperty("confirmation_token").GetString();
                var answer = MessageBox.Show(
                    this,
                    $"私人记忆已成功导出到：\n{exportPath}\n\n是否彻底清空整个私人记忆空间？这会删除正式消息、摘要、索引和派生关系，且不可撤销；持仓、成交、日程和任务不会被删除。",
                    "二次确认：清空私人记忆", MessageBoxButton.YesNo, MessageBoxImage.Warning, MessageBoxResult.No);
                await _companionExchange.SendAsync(new
                {
                    contract = "memory-user-command/v1", command_id = Guid.NewGuid().ToString(), type = "memory.clear",
                    confirmed = answer == MessageBoxResult.Yes, confirmation_token = token,
                });
            }
            else if (state == "cleared")
            {
                MessageBox.Show(this, "私人记忆空间已清空。MarketHub、8815、持仓、成交、日程和任务未受影响。", "AI交易伙伴", MessageBoxButton.OK, MessageBoxImage.Information);
            }
        }
    }

    private void RenderHistoricalMessage()
    {
        var body = _viewModel.SelectedMessage?.BodyMarkdown;
        if (string.IsNullOrWhiteSpace(body)) RenderAiMessages([]);
        else RenderAiMessages([new CompanionAiTimelineEntry("history", "history", DateTimeOffset.Now, body)]);
    }

    private void RenderAiMessages(IReadOnlyList<CompanionAiTimelineEntry> messages)
    {
        var wasAtBottom = AiTimelineScrollViewer.ScrollableHeight <= 0
            || AiTimelineScrollViewer.VerticalOffset >= AiTimelineScrollViewer.ScrollableHeight - 36;
        AiTimelinePanel.Children.Clear();
        _aiAnchors.Clear();
        foreach (var message in messages.OrderBy(item => item.At))
        {
            if (message.Kind is "chat_pending" or "action_pending")
            {
                var pending = new StackPanel { Orientation = System.Windows.Controls.Orientation.Horizontal, Margin = new Thickness(4, 2, 0, 12) };
                pending.Children.Add(new TextBlock
                {
                    Text = "正在想…", Foreground = (Brush)FindResource("SecondaryTextBrush"), FontSize = 13,
                });
                if (message.Kind == "chat_pending" && _companionProjection is not null)
                {
                    var stop = new Button { Content = "终止", Tag = _companionProjection.CycleId, Padding = new Thickness(8, 2, 8, 2), Margin = new Thickness(10, -3, 0, 0) };
                    stop.Click += async (_, _) => await SendCompanionCommandAsync("terminate_chat_research", null, showAiError: true);
                    pending.Children.Add(stop);
                }
                AiTimelinePanel.Children.Add(pending);
                continue;
            }
            var timing = new TextBlock
            {
                Text = FormatTiming(message), Foreground = (Brush)FindResource("SecondaryTextBrush"),
                FontSize = 11, Margin = new Thickness(0, 3, 0, 0),
            };
            var body = CreatePublishedMessageViewer(message);
            var headerRow = new Grid();
            headerRow.ColumnDefinitions.Add(new ColumnDefinition());
            headerRow.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
            var copy = CreateCopyButton(message.Text);
            Grid.SetColumn(copy, 1);
            headerRow.Children.Add(copy);
            if (message.Kind == "chat_terminated" && _companionProjection is not null)
            {
                var resume = new Button { Content = "继续研究", Tag = _companionProjection.CycleId, Padding = new Thickness(8, 2, 8, 2) };
                resume.Click += async (_, _) => await SendCompanionCommandAsync("continue_chat_research", null, showAiError: true);
                Grid.SetColumn(resume, 1); headerRow.Children.Add(resume);
            }
            var content = new StackPanel { Children = { headerRow, timing, body } };
            var card = new Border
            {
                Background = (Brush)FindResource("CardBrush"), BorderBrush = (Brush)FindResource("BorderBrush"),
                BorderThickness = new Thickness(1), CornerRadius = new CornerRadius(9), Padding = new Thickness(12),
                Margin = new Thickness(0, 0, 0, 10), Child = content, Cursor = Cursors.Hand,
            };
            card.MouseLeftButtonDown += (_, _) =>
            {
                _activeAiMarkdown = message.Text;
                ReadAloudButton.IsEnabled = true;
            };
            AiTimelinePanel.Children.Add(card);
            if (message.Kind is "premarket" or "premarket_chat" or "m0" or "m1" or "m2")
                _aiAnchors[message.Kind == "premarket_chat" ? "premarket" : message.Kind] = card;
        }
        if (messages.Count == 0)
        {
            AiTimelinePanel.Children.Add(new TextBlock
            {
                Text = "当前判断尚未产生 AI 消息。", Foreground = (Brush)FindResource("SecondaryTextBrush"),
                TextWrapping = TextWrapping.Wrap,
            });
            _activeAiMarkdown = null;
        }
        else _activeAiMarkdown = messages.LastOrDefault(message => message.Kind is not ("chat_pending" or "action_pending"))?.Text;
        ReadAloudButton.IsEnabled = !string.IsNullOrWhiteSpace(_activeAiMarkdown);
        if (wasAtBottom) Dispatcher.BeginInvoke(() => AiTimelineScrollViewer.ScrollToEnd(), DispatcherPriority.Loaded);
    }

    private void SetLocalAiNotice(string cycleId, string kind, string text)
    {
        _localAiNoticesByCycle[cycleId] = new CompanionAiTimelineEntry(
            $"local-reply-status-{cycleId}", kind, DateTimeOffset.Now, text, DateTimeOffset.Now);
        if (_companionProjection?.CycleId == cycleId)
            RenderAiMessages(WithLocalAiNotice(cycleId, _companionProjection.AiMessages));
    }

    private IReadOnlyList<CompanionAiTimelineEntry> WithLocalAiNotice(
        string cycleId,
        IReadOnlyList<CompanionAiTimelineEntry> messages)
    {
        if (!_localAiNoticesByCycle.TryGetValue(cycleId, out var notice))
        {
            if (_companionProjection?.CycleId != cycleId || !_companionProjection.IsCompanionThinking) return messages;
            notice = new CompanionAiTimelineEntry(
                $"projection-reply-status-{cycleId}", "chat_pending", DateTimeOffset.Now, "正在想…", DateTimeOffset.Now);
        }
        return [.. messages, notice];
    }

    private void ResolveLocalAiNotice(string cycleId, IReadOnlyList<CompanionAiTimelineEntry> messages)
    {
        if (!_localAiNoticesByCycle.TryGetValue(cycleId, out var notice) || notice.Kind == "fault") return;
        if (messages.Any(message => message.At >= notice.At))
            _localAiNoticesByCycle.Remove(cycleId);
    }

    private void RenderUserMessages()
    {
        MainJudgmentTimelinePanel.Children.Clear();
        var messages = CombinedUserMessages().OrderBy(message => message.At).ToArray();
        foreach (var entry in messages)
        {
            var isStaged = entry.State == "staged";
            var headerText = isStaged ? $"待提交 · {entry.At.ToLocalTime():HH:mm}"
                : entry.Phase == "h0" ? $"H0 · {entry.At.ToLocalTime():HH:mm}"
                : entry.Phase == "pre_m0" ? $"盘前交流 · {entry.At.ToLocalTime():HH:mm}"
                : $"我 · {entry.At.ToLocalTime():HH:mm}";
            var header = new TextBlock
            {
                Text = headerText, FontSize = 11,
                Foreground = isStaged ? (Brush)FindResource("BlueBrush") : (Brush)FindResource("SecondaryTextBrush"),
            };
            var body = CreateMarkdownViewer(entry.Text, new Thickness(0, 3, 0, 0));
            var headerRow = new Grid();
            headerRow.ColumnDefinitions.Add(new ColumnDefinition());
            headerRow.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
            var copy = CreateCopyButton(entry.Text);
            Grid.SetColumn(copy, 1);
            headerRow.Children.Add(header);
            headerRow.Children.Add(copy);
            var content = new StackPanel { Children = { headerRow, body } };
            if (entry.ArtifactId is { Length: > 0 } artifactId
                && _portfolioProjection?.StatusByArtifactId.TryGetValue(artifactId, out var status) == true)
            {
                content.Children.Add(new TextBlock
                {
                    Text = status, Foreground = (Brush)FindResource("AccentBrush"), FontSize = 11,
                    Margin = new Thickness(0, 7, 0, 0), TextWrapping = TextWrapping.Wrap,
                });
            }
            if (isStaged && entry.MessageId is { } messageId)
            {
                var actions = new StackPanel { Orientation = System.Windows.Controls.Orientation.Horizontal, HorizontalAlignment = HorizontalAlignment.Right, Margin = new Thickness(0, 7, 0, 0) };
                var edit = new Button
                {
                    Content = "编辑", Tag = entry, Padding = new Thickness(8, 3, 8, 3),
                };
                edit.Click += EditStaged_Click;
                actions.Children.Add(edit);
                var withdraw = new Button
                {
                    Content = "撤回", Tag = messageId, HorizontalAlignment = HorizontalAlignment.Right,
                    Padding = new Thickness(8, 3, 8, 3), Margin = new Thickness(7, 0, 0, 0),
                };
                withdraw.Click += WithdrawStaged_Click;
                actions.Children.Add(withdraw);
                content.Children.Add(actions);
            }
            MainJudgmentTimelinePanel.Children.Add(new Border
            {
                Background = isStaged ? new SolidColorBrush(Color.FromRgb(21, 42, 66)) : Brushes.Transparent,
                BorderBrush = (Brush)FindResource("BorderBrush"), BorderThickness = new Thickness(1),
                CornerRadius = new CornerRadius(8), Padding = new Thickness(10, 8, 10, 8),
                Margin = new Thickness(0, 0, 0, 8), Child = content,
            });
        }
        if (messages.Length == 0)
            MainJudgmentTimelinePanel.Children.Add(new TextBlock { Text = "当前判断还没有你的消息。", Foreground = (Brush)FindResource("SecondaryTextBrush") });
    }

    private UIElement CreatePublishedMessageViewer(CompanionAiTimelineEntry message)
    {
        if (message.Parts is null || message.Parts.Count == 0)
            return CreateMarkdownViewer(message.Text, new Thickness(0, 8, 0, 0));

        var panel = new StackPanel { Margin = new Thickness(0, 8, 0, 0) };
        foreach (var part in message.Parts)
        {
            if (part.Kind == "material")
            {
                var material = new StackPanel();
                if (!string.IsNullOrWhiteSpace(part.SourceTitle))
                    material.Children.Add(new TextBlock
                    {
                        Text = part.SourceTitle, FontWeight = FontWeights.SemiBold,
                        Foreground = (Brush)FindResource("SecondaryTextBrush"),
                    });
                material.Children.Add(CreateMarkdownViewer(part.Text, new Thickness(0, 5, 0, 0)));
                panel.Children.Add(new Border
                {
                    BorderBrush = (Brush)FindResource("BorderBrush"), BorderThickness = new Thickness(1, 0, 0, 0),
                    Padding = new Thickness(10, 2, 0, 2), Margin = new Thickness(0, 7, 0, 0), Child = material,
                });
            }
            else panel.Children.Add(CreateMarkdownViewer(part.Text, new Thickness(0)));
        }
        return panel;
    }

    private static FlowDocumentScrollViewer CreateMarkdownViewer(string markdown, Thickness margin)
    {
        var document = MarkdownDocumentBuilder.Build(markdown);
        document.ColumnWidth = double.PositiveInfinity;
        var viewer = new FlowDocumentScrollViewer
        {
            Document = document,
            IsToolBarVisible = false,
            VerticalScrollBarVisibility = ScrollBarVisibility.Hidden,
            HorizontalScrollBarVisibility = ScrollBarVisibility.Disabled,
            Background = Brushes.Transparent,
            BorderThickness = new Thickness(0),
            Padding = new Thickness(0),
            Margin = margin,
            IsSelectionEnabled = true,
        };
        NestedScrollWheelForwarder.Attach(viewer);
        return viewer;
    }

    private Button CreateCopyButton(string text)
    {
        var button = new Button
        {
            Content = "复制", Tag = text, FontSize = 11,
            Padding = new Thickness(7, 2, 7, 2), Margin = new Thickness(8, 0, 0, 0),
            HorizontalAlignment = HorizontalAlignment.Right, VerticalAlignment = VerticalAlignment.Top,
            ToolTip = "复制消息原文",
        };
        button.Click += CopyMessage_Click;
        return button;
    }

    private async void CopyMessage_Click(object sender, RoutedEventArgs e)
    {
        if (sender is not Button { Tag: string text }) return;
        try { await ClipboardCopyService.CopyTextAsync(text); }
        catch (Exception exception)
        {
            MessageBox.Show(this, $"复制失败：{exception.Message}", "AI交易伙伴",
                MessageBoxButton.OK, MessageBoxImage.Warning);
        }
    }

    private IEnumerable<CompanionTimelineEntry> CombinedUserMessages()
    {
        var projected = _companionProjection?.UserMessages ?? [];
        return projected.Concat(LocalStaged())
            .Where(message => message.MessageId is null || !_locallyWithdrawnMessageIds.Contains(message.MessageId))
            .GroupBy(message => message.MessageId ?? $"{message.At:O}-{message.Text}", StringComparer.Ordinal)
            .Select(group => group.Last());
    }

    private List<CompanionTimelineEntry> LocalStaged()
    {
        var cycleId = _companionProjection?.CycleId;
        if (cycleId is null) return [];
        if (!_localStagedByCycle.TryGetValue(cycleId, out var pending))
        {
            pending = [];
            _localStagedByCycle[cycleId] = pending;
        }
        return pending;
    }

    private bool IsH0LockedForUi() => _companionProjection is not null
        && (_companionProjection.IsH0Locked || _locallyLockedCycles.Contains(_companionProjection.CycleId));

    private void AiAnchor_Click(object sender, RoutedEventArgs e)
    {
        if (sender is not Button { Tag: string tag }) return;
        if (tag == "latest") AiTimelineScrollViewer.ScrollToEnd();
        else if (_aiAnchors.TryGetValue(tag, out var target)) target.BringIntoView();
    }

    private async void RequestCompanionProjectionAsync(string cycleId)
    {
        if (_requestedProjectionCycleId == cycleId) return;
        _requestedProjectionCycleId = cycleId;
        try
        {
            await _companionExchange.SendAsync(new
            {
                contract = "companion-user-command/v1", command_id = Guid.NewGuid().ToString(),
                cycle_id = cycleId, type = "request_projection",
            });
        }
        catch (Exception exception) { _viewModel.ReportInboxFailure(exception); }
    }

    private async void RequestTodayProjectionsAsync()
    {
        try
        {
            await _viewModel.RefreshTodayBoardAsync();
        }
        catch (Exception exception)
        {
            _viewModel.ReportInboxFailure(exception);
        }
    }

    private void HistoryTree_SelectedItemChanged(object sender, RoutedPropertyChangedEventArgs<object> e)
    {
        if (e.NewValue is not HistoryRecordViewModel record) return;
        _viewModel.SelectedSectionIndex = 1;
        _viewModel.SelectedHistory = record;
    }

    private async void MainJudgmentInputBox_TextChanged(object sender, TextChangedEventArgs e)
    {
        if (!_suppressDraftUpdate && _displayedDraftCycleId is not null)
        {
            _judgmentDrafts[_displayedDraftCycleId] = MainJudgmentInputBox.Text;
            CompanionDraftStore.Save(_paths, _judgmentDrafts);
        }
        if (!_suppressDraftUpdate
            && !string.IsNullOrWhiteSpace(MainJudgmentInputBox.Text)
            && _companionProjection is { } projection
            && projection.State != "queued"
            && !IsH0LockedForUi()
            && _editGraceRequestedCycles.Add(projection.CycleId))
        {
            try { await SendCompanionCommandAsync("begin_h0_edit", null); }
            catch { _editGraceRequestedCycles.Remove(projection.CycleId); }
        }
        UpdateInputState();
    }

    private void SwitchDraft(string? cycleId)
    {
        if (string.Equals(_displayedDraftCycleId, cycleId, StringComparison.Ordinal)) return;
        if (_displayedDraftCycleId is not null) _judgmentDrafts[_displayedDraftCycleId] = MainJudgmentInputBox.Text;
        _displayedDraftCycleId = cycleId;
        _suppressDraftUpdate = true;
        MainJudgmentInputBox.Text = cycleId is not null && _judgmentDrafts.TryGetValue(cycleId, out var draft) ? draft : string.Empty;
        MainJudgmentInputBox.CaretIndex = MainJudgmentInputBox.Text.Length;
        _suppressDraftUpdate = false;
    }

    private void AppendToDraft(string? cycleId, string text)
    {
        if (string.IsNullOrWhiteSpace(cycleId) || string.IsNullOrWhiteSpace(text)) return;
        var existing = _judgmentDrafts.GetValueOrDefault(cycleId, string.Empty);
        var updated = string.IsNullOrWhiteSpace(existing) ? text.Trim() : $"{existing.TrimEnd()}\n{text.Trim()}";
        _judgmentDrafts[cycleId] = updated;
        CompanionDraftStore.Save(_paths, _judgmentDrafts);
        if (!string.Equals(_displayedDraftCycleId, cycleId, StringComparison.Ordinal)) return;
        _suppressDraftUpdate = true;
        MainJudgmentInputBox.Text = updated;
        MainJudgmentInputBox.CaretIndex = updated.Length;
        _suppressDraftUpdate = false;
    }

    private void SetVoiceState(VoiceInputState state)
    {
        _voiceState = state;
        if (state != VoiceInputState.Recording)
        {
            _waveformLevels.Clear();
            WaveformLine.Points.Clear();
        }
        UpdateInputState();
    }

    private void UpdateInputState()
    {
        if (!IsInitialized) return;
        var busy = _voiceState is VoiceInputState.Recording or VoiceInputState.Transcribing;
        var hasCycle = _companionProjection is not null;
        var supportsMessaging = CompanionInputPolicy.CanDraft(_companionProjection?.State);
        var h0Locked = IsH0LockedForUi();
        var staged = CombinedUserMessages().Count(message => message.State == "staged");
        MainJudgmentInputBox.IsReadOnly = busy || !supportsMessaging;
        MainSendButton.IsEnabled = !busy && hasCycle && supportsMessaging && !string.IsNullOrWhiteSpace(MainJudgmentInputBox.Text);
        MainCommitButton.Content = CompanionInputPolicy.CommitLabel(_companionProjection?.State, h0Locked);
        MainCommitButton.IsEnabled = !busy && hasCycle
            && CompanionInputPolicy.CanCommit(_companionProjection?.State, h0Locked, staged);
        MainVoiceButton.IsEnabled = hasCycle && supportsMessaging && _voiceState != VoiceInputState.Transcribing;
        MainVoiceButton.Content = _voiceState switch
        {
            VoiceInputState.Recording => "停止并转写",
            VoiceInputState.Transcribing => "正在转写…",
            VoiceInputState.Error => "重试语音",
            _ => "语音输入",
        };
        WaveformCanvas.Visibility = _voiceState == VoiceInputState.Recording ? Visibility.Visible : Visibility.Collapsed;
        InputStatusText.Text = InputStatus(h0Locked, staged);
    }

    private string InputStatus(bool h0Locked, int staged)
    {
        if (_companionProjection is null) return "请选择已经启动的当前判断。";
        if (_companionProjection.State is "model_only_ready" or "joint_ready" or "missed" or "failed")
            return "这是旧链路或未完成周期，仅供查看。";
        if (_companionProjection.State == "queued")
            return staged > 0
                ? $"{staged} 条盘前消息待冻结；M0 开始时会作为待核验线索。"
                : "现在可以盘前交流；消息可能影响 M0 的搜索重点，但 AI 会独立核验。";
        if (_companionProjection.State == "open")
            return staged > 0 ? $"{staged} 条消息待提交；发送前都可以修改或撤回。" : "可以随时聊天；未发送草稿会一直保留。";
        if (h0Locked)
        {
            if (_companionProjection.State is "researching_m1" or "judging_m1" or "m1_retry_wait")
                return staged > 0 ? $"M1 正在独立生成；{staged} 条消息待提交。" : "M1 正在独立生成。";
            return staged > 0 ? $"{staged} 条消息待提交，提交后 AI 才会回复。" : "发送后可一次提交一批消息。";
        }
        if (_companionProjection.H0AutoSubmitAt is not { } deadline)
            return staged > 0 ? $"{staged} 条消息等待提交 H0。" : "可以直接提交 H0 表示不评论。";
        var remaining = deadline - DateTimeOffset.Now;
        if (remaining <= TimeSpan.Zero) return "H0 正在自动提交；输入框草稿不会发送。";
        var countdown = remaining.TotalHours >= 1 ? $"{(int)remaining.TotalHours}小时{remaining.Minutes}分" : $"{Math.Max(0, remaining.Minutes)}分{remaining.Seconds:00}秒";
        return staged > 0
            ? $"{staged} 条消息待提交；{deadline.ToLocalTime():HH:mm} 自动提交（{countdown}）。"
            : $"{deadline.ToLocalTime():HH:mm} 自动按不评论提交（{countdown}）。";
    }

    private void OnVoiceLevelChanged(object? sender, double level) => _ = Dispatcher.InvokeAsync(() =>
    {
        if (_voiceState != VoiceInputState.Recording) return;
        _waveformLevels.Enqueue(Math.Max(0.03, Math.Sqrt(level)));
        while (_waveformLevels.Count > 72) _waveformLevels.Dequeue();
        var width = Math.Max(1, WaveformCanvas.ActualWidth);
        var height = Math.Max(1, WaveformCanvas.ActualHeight);
        var values = _waveformLevels.ToArray();
        var points = new PointCollection();
        for (var index = 0; index < values.Length; index++)
        {
            var x = values.Length == 1 ? width / 2 : index * width / (values.Length - 1);
            var amplitude = values[index] * height * 0.38;
            points.Add(new Point(x, height / 2 - amplitude));
            points.Add(new Point(x, height / 2 + amplitude));
        }
        WaveformLine.Points = points;
    });

    private static string FormatTiming(CompanionAiTimelineEntry message)
    {
        var started = (message.StartedAt ?? message.At).ToLocalTime().ToString("HH:mm:ss", CultureInfo.InvariantCulture);
        var completed = (message.CompletedAt ?? message.At).ToLocalTime().ToString("HH:mm:ss", CultureInfo.InvariantCulture);
        return $"开始 {started} · 完成 {completed}";
    }

    private string BuildAsrContext()
    {
        var ai = _companionProjection?.AiMessages.Select(message => message.Text) ?? [];
        var mine = CombinedUserMessages().Select(message => message.Text);
        var draft = string.IsNullOrWhiteSpace(_voiceOriginCycleId)
            ? string.Empty
            : _judgmentDrafts.GetValueOrDefault(_voiceOriginCycleId, string.Empty);
        return string.Join(Environment.NewLine, ai.Concat(mine).Append(draft).Where(text => !string.IsNullOrWhiteSpace(text)));
    }

    private async Task<string> TranscribeAsync(string audio, string? contextFile)
    {
        var installRoot = ProjectRoot();
        var output = Path.Combine(_paths.DataDirectory, "ui", "asr", $"{Path.GetFileNameWithoutExtension(audio)}.json");
        var contextArgument = string.IsNullOrWhiteSpace(contextFile) ? string.Empty : $" --context-file \"{contextFile}\"";
        var runtimePython = Path.Combine(_paths.DataDirectory, "runtime", "python", "Scripts", "python.exe");
        var python = File.Exists(runtimePython) ? runtimePython : "py";
        var info = new ProcessStartInfo(python, $"-m ai_trading_companion.asr --audio \"{audio}\" --output \"{output}\"{contextArgument}")
        {
            UseShellExecute = false,
            RedirectStandardError = true,
            CreateNoWindow = true,
            WorkingDirectory = installRoot,
        };
        info.Environment["PYTHONPATH"] = Path.Combine(installRoot, "runtime");
        info.Environment["AI_TRADING_COMPANION_INSTALL_ROOT"] = installRoot;
        info.Environment["AI_TRADING_COMPANION_HOME"] = _paths.DataDirectory;
        using var process = Process.Start(info) ?? throw new InvalidOperationException("Unable to start local ASR process.");
        await process.WaitForExitAsync();
        if (process.ExitCode != 0) throw new InvalidOperationException((await process.StandardError.ReadToEndAsync()).Trim());
        using var document = JsonDocument.Parse(await File.ReadAllTextAsync(output));
        return document.RootElement.GetProperty("corrected_text").GetString() ?? string.Empty;
    }

    private static string ProjectRoot() => Environment.GetEnvironmentVariable("AI_TRADING_COMPANION_INSTALL_ROOT") ?? AppContext.BaseDirectory;

    private void OnSpeechStateChanged(object? sender, EventArgs e) => _ = Dispatcher.InvokeAsync(() =>
    {
        (ReadAloudButton.Content, ReadAloudButton.ToolTip) = _speech.State switch
        {
            SpeechState.Generating => ("停止生成", $"正在用 {_speech.VoiceName} 生成语音；点击停止"),
            SpeechState.Playing => ("停止朗读", $"停止 {_speech.VoiceName} 的朗读"),
            _ => ("朗读", $"使用 {_speech.VoiceName} 朗读 AI 消息"),
        };
    });

    private async void PortfolioButton_Click(object sender, RoutedEventArgs e)
    {
        if (_portfolioWindow is null)
        {
            _portfolioWindow = new PortfolioWindow(UndoLatestPortfolioChangeAsync) { Owner = this };
            _portfolioWindow.Closed += (_, _) => _portfolioWindow = null;
            _portfolioWindow.Show();
        }
        else
        {
            if (_portfolioWindow.WindowState == WindowState.Minimized) _portfolioWindow.WindowState = WindowState.Normal;
            _portfolioWindow.Activate();
        }
        _portfolioWindow.UpdateProjection(_portfolioProjection);
        await SendPortfolioCommandAsync("request_snapshot");
    }

    private void TaskButton_Click(object sender, RoutedEventArgs e)
    {
        if (_taskManagementWindow is null)
        {
            _taskManagementWindow = new TaskManagementWindow(_paths, _companionExchange) { Owner = this };
            _taskManagementWindow.Closed += (_, _) => _taskManagementWindow = null;
            _taskManagementWindow.Show();
        }
        else
        {
            if (_taskManagementWindow.WindowState == WindowState.Minimized) _taskManagementWindow.WindowState = WindowState.Normal;
            _taskManagementWindow.Activate();
        }
    }

    private void ToolManagerButton_Click(object sender, RoutedEventArgs e)
    {
        var executable = Path.Combine(ProjectRoot(), "ToolManager", "AITradingCompanion.ToolManager.exe");
        if (!File.Exists(executable))
        {
            MessageBox.Show("工具管理程序尚未安装。请重新发布 AI 交易伙伴。", "工具管理", MessageBoxButton.OK, MessageBoxImage.Information);
            return;
        }
        Process.Start(new ProcessStartInfo(executable) { UseShellExecute = true });
    }

    private void ObservatoryButton_Click(object sender, RoutedEventArgs e)
    {
        if (_evaluationObservatoryWindow is null)
        {
            _evaluationObservatoryWindow = new EvaluationObservatoryWindow(_companionExchange) { Owner = this };
            _evaluationObservatoryWindow.Closed += (_, _) => _evaluationObservatoryWindow = null;
            _evaluationObservatoryWindow.Show();
        }
        else
        {
            if (_evaluationObservatoryWindow.WindowState == WindowState.Minimized)
                _evaluationObservatoryWindow.WindowState = WindowState.Normal;
            _evaluationObservatoryWindow.Activate();
        }
    }

    private Task UndoLatestPortfolioChangeAsync() => SendPortfolioCommandAsync("revert_transaction");

    private Task SendPortfolioCommandAsync(string type) => _companionExchange.SendAsync(new
    {
        contract = "portfolio-user-command/v1", command_id = Guid.NewGuid().ToString(), type,
    });

    private void RestoreSavedSize()
    {
        if (WindowStateService.Load(_paths) is not { } saved) return;
        var workArea = SystemParameters.WorkArea;
        Width = Math.Clamp(saved.Width, MinWidth, Math.Max(MinWidth, workArea.Width));
        Height = Math.Clamp(saved.Height, MinHeight, Math.Max(MinHeight, workArea.Height));
        if (saved.Left is not { } left || saved.Top is not { } top) return;
        WindowStartupLocation = WindowStartupLocation.Manual;
        Left = Math.Clamp(left, workArea.Left, Math.Max(workArea.Left, workArea.Right - Width));
        Top = Math.Clamp(top, workArea.Top, Math.Max(workArea.Top, workArea.Bottom - Height));
    }

    private void OnSizeChanged(object sender, SizeChangedEventArgs e) { _saveSizeTimer.Stop(); _saveSizeTimer.Start(); }
    private void OnLocationChanged(object? sender, EventArgs e) { _saveSizeTimer.Stop(); _saveSizeTimer.Start(); }
    private void OnSaveSizeTimerTick(object? sender, EventArgs e) { _saveSizeTimer.Stop(); SaveCurrentSize(); }

    private void SaveCurrentSize()
    {
        var bounds = WindowState == WindowState.Normal ? new Rect(Left, Top, ActualWidth, ActualHeight) : RestoreBounds;
        WindowStateService.Save(_paths, bounds.Width, bounds.Height, bounds.Left, bounds.Top);
    }

    private enum VoiceInputState { Idle, Recording, Transcribing, Error }
}
