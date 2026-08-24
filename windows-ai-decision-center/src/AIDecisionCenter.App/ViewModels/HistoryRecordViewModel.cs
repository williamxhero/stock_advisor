using System.Globalization;
using AIDecisionCenter.Core.Models;

namespace AIDecisionCenter.App.ViewModels;

public sealed class HistoryRecordViewModel
{
    public HistoryRecordViewModel(TaskMessage message)
    {
        Message = message;
    }

    public TaskMessage Message { get; }

    public string TaskType => Message.TaskType;

    public string CategoryText => $"{Message.Project} · 节点 {Message.Slot.ToString("HH:mm", CultureInfo.InvariantCulture)}";

    public string ReceivedAtText => Message.ReceivedAt
        .ToLocalTime()
        .ToString("yyyy-MM-dd HH:mm:ss", CultureInfo.InvariantCulture);

    public bool IsUnread => !Message.IsRead;
}
