using AITradingCompanion.Core.Models;
using System.Globalization;

namespace AITradingCompanion.Desktop.ViewModels;

public sealed class TaskRowViewModel : ObservableObject
{
    private readonly TimeSpan _nodeTimeout;
    private TaskMessage? _message;
    private DateTime _statusAsOf;
    private string? _companionState;
    private string? _companionError;

    public TaskRowViewModel(ExpectedTask expected, TaskMessage? message, TimeSpan nodeTimeout)
    {
        ArgumentOutOfRangeException.ThrowIfLessThanOrEqual(nodeTimeout, TimeSpan.Zero);
        Expected = expected;
        _message = message;
        _nodeTimeout = nodeTimeout;
        _statusAsOf = DateTime.Now;
    }

    public ExpectedTask Expected { get; }

    public TaskMessage? Message
    {
        get => _message;
        set
        {
            if (SetProperty(ref _message, value))
            {
                OnPropertyChanged(nameof(IsComplete));
                OnPropertyChanged(nameof(IsUnread));
                OnPropertyChanged(nameof(IsPassed));
                OnPropertyChanged(nameof(HasRuntimeProblem));
                OnPropertyChanged(nameof(ShowCompletedIndicator));
                OnPropertyChanged(nameof(StatusText));
            }
        }
    }

    public string TimeText => Expected.Slot.ToString("HH:mm", CultureInfo.InvariantCulture);

    public string Name => Message?.TaskType ?? Expected.Name;

    public bool IsComplete => Message is not null || _companionState is "complete" or "m1_ready" or "m2_deferred";

    public bool IsUnread => Message is { IsRead: false };

    public bool IsPassed => _companionState is null
        && _statusAsOf >= _statusAsOf.Date.Add(Expected.Slot.ToTimeSpan()).Add(_nodeTimeout);

    public bool HasRuntimeProblem => _companionState is "failed" or "missed" or "waiting_for_repair" || IsPassed;

    public bool ShowCompletedIndicator => IsComplete && !HasRuntimeProblem;

    public string StatusText => _companionState switch
    {
        "open" => "随时可以聊天",
        "queued" => "可输入盘前消息",
        "researching" or "researching_m0" => "AI 研究中",
        "awaiting_h0" or "voice_grace" => "等待提交 H0",
        "h0_locked" or "researching_m1" or "judging_m1" or "m1_retry_wait" => "正在生成 M1",
        "synthesizing_m2" => "正在生成 M2",
        "m2_deferred" => "M2 已延后",
        "m1_ready" or "complete" => "AI 判断已完成",
        "waiting_for_repair" => "等待修复",
        "failed" => "AI 运行失败",
        "missed" => "未按时运行",
        _ => Message?.Status switch
        {
            TaskMessageStatus.Succeeded => IsPassed ? "正式消息已到，AI 未启动" : "正式消息已到",
            TaskMessageStatus.Skipped => "已跳过",
            TaskMessageStatus.Failed => "正式任务失败",
            _ => IsPassed ? "AI 未启动" : "等待触发"
        }
    };

    public string? RuntimeError => _companionError;

    public void UpdateCompanionStatus(string? state, string? error)
    {
        if (_companionState == state && _companionError == error)
        {
            return;
        }
        _companionState = state;
        _companionError = error;
        OnPropertyChanged(nameof(IsComplete));
        OnPropertyChanged(nameof(IsPassed));
        OnPropertyChanged(nameof(HasRuntimeProblem));
        OnPropertyChanged(nameof(ShowCompletedIndicator));
        OnPropertyChanged(nameof(StatusText));
        OnPropertyChanged(nameof(RuntimeError));
    }

    public string ReceivedAtText => Message is null
        ? string.Empty
        : Message.ReceivedAt.ToLocalTime().ToString("yyyy-MM-dd HH:mm:ss", CultureInfo.InvariantCulture);

    public void RefreshStatus(DateTime now)
    {
        _statusAsOf = now;
        OnPropertyChanged(nameof(IsPassed));
        OnPropertyChanged(nameof(HasRuntimeProblem));
        OnPropertyChanged(nameof(ShowCompletedIndicator));
        OnPropertyChanged(nameof(StatusText));
    }
}
