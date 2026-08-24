using AIDecisionCenter.Core.Models;
using System.Globalization;

namespace AIDecisionCenter.App.ViewModels;

public sealed class TaskRowViewModel : ObservableObject
{
    private readonly TimeSpan _nodeTimeout;
    private TaskMessage? _message;
    private DateTime _statusAsOf;

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
                OnPropertyChanged(nameof(StatusText));
            }
        }
    }

    public string TimeText => Expected.Slot.ToString("HH:mm", CultureInfo.InvariantCulture);

    public string Name => Message?.TaskType ?? Expected.Name;

    public bool IsComplete => Message is not null;

    public bool IsUnread => Message is { IsRead: false };

    public bool IsPassed => Message is null
        && _statusAsOf >= _statusAsOf.Date.Add(Expected.Slot.ToTimeSpan()).Add(_nodeTimeout);

    public string StatusText => IsComplete ? "已完成" : IsPassed ? "PASS" : "等待";

    public string ReceivedAtText => Message is null
        ? string.Empty
        : Message.ReceivedAt.ToLocalTime().ToString("yyyy-MM-dd HH:mm:ss", CultureInfo.InvariantCulture);

    public void RefreshStatus(DateTime now)
    {
        _statusAsOf = now;
        OnPropertyChanged(nameof(IsPassed));
        OnPropertyChanged(nameof(StatusText));
    }
}
