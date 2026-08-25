using AIDecisionCenter.App.ViewModels;
using AIDecisionCenter.Core.Models;

namespace AIDecisionCenter.Tests;

public sealed class TaskRowViewModelTests
{
    [Fact]
    public void MissingTaskBecomesPassedAtDeadline()
    {
        var row = new TaskRowViewModel(
            new ExpectedTask("daily.opportunity.0900", new TimeOnly(9, 0), "盘前机会发现"),
            null,
            TimeSpan.FromMinutes(20));

        row.RefreshStatus(new DateTime(2026, 8, 24, 9, 20, 0));

        Assert.True(row.IsPassed);
        Assert.Equal("AI 未启动", row.StatusText);
        Assert.True(row.HasRuntimeProblem);
    }

    [Fact]
    public void FailedMessageIsReceivedButShowsFailure()
    {
        var incoming = TestMessageFactory.CreateIncoming();
        var message = new TaskMessage(
            1, incoming.ExternalId, incoming.Source, incoming.SourceRunId, incoming.Project, incoming.TaskKey,
            new TimeOnly(14, 30), incoming.TaskType, new DateOnly(2026, 8, 24), incoming.ScheduledFor,
            incoming.CompletedAt, incoming.ReceivedAt, TaskMessageStatus.Failed, incoming.RegistryId,
            incoming.ProtocolId, incoming.Summary, incoming.BodyMarkdown, incoming.PayloadJson,
            incoming.ContentSha256, false, false, false, string.Empty);
        var row = new TaskRowViewModel(
            new ExpectedTask("daily.execution.1430", new TimeOnly(14, 30), "操作决策"),
            message,
            TimeSpan.FromMinutes(20));

        Assert.True(row.IsComplete);
        Assert.Equal("正式任务失败", row.StatusText);
    }

    [Fact]
    public void CompanionFailureOverridesSuccessfulFormalMessage()
    {
        var incoming = TestMessageFactory.CreateIncoming();
        var message = new TaskMessage(
            1, incoming.ExternalId, incoming.Source, incoming.SourceRunId, incoming.Project, incoming.TaskKey,
            new TimeOnly(14, 30), incoming.TaskType, new DateOnly(2026, 8, 24), incoming.ScheduledFor,
            incoming.CompletedAt, incoming.ReceivedAt, TaskMessageStatus.Succeeded, incoming.RegistryId,
            incoming.ProtocolId, incoming.Summary, incoming.BodyMarkdown, incoming.PayloadJson,
            incoming.ContentSha256, false, false, false, string.Empty);
        var row = new TaskRowViewModel(
            new ExpectedTask("daily.execution.1430", new TimeOnly(14, 30), "操作决策"),
            message,
            TimeSpan.FromMinutes(20));

        row.UpdateCompanionStatus("missed", "服务恢复时已超过补偿窗口");

        Assert.Equal("未按时运行", row.StatusText);
        Assert.True(row.HasRuntimeProblem);
        Assert.False(row.ShowCompletedIndicator);
    }
}
