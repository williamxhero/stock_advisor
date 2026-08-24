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

    public string CategoryText => $"{Message.Project} · {StatusText} · {Message.Slot.ToString("HH:mm", CultureInfo.InvariantCulture)}";

    public string Summary => Message.Summary;

    public string StatusText => Message.Status switch
    {
        TaskMessageStatus.Succeeded => "正常",
        TaskMessageStatus.Skipped => "已跳过",
        TaskMessageStatus.Failed => "失败",
        _ => string.Empty
    };

    public string ReceivedAtText => Message.ReceivedAt
        .ToLocalTime()
        .ToString("yyyy-MM-dd HH:mm:ss", CultureInfo.InvariantCulture);

    public bool IsUnread => !Message.IsRead;

    public bool IsStarred => Message.IsStarred;
}
