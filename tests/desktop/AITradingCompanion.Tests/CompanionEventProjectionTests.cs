using AITradingCompanion.Desktop.Services;
using System.Globalization;

namespace AITradingCompanion.Tests;

public sealed class CompanionEventProjectionTests
{
    [Fact]
    public void RuntimeExchangeDesktopReleaseFixtureUsesTheSealedV2Text()
    {
        var json = File.ReadAllText(Path.Combine(AppContext.BaseDirectory, "fixtures", "message-v2-runtime-exchange-desktop.json"));

        var projection = CompanionEventProjection.Project([json]);

        Assert.NotNull(projection);
        var message = Assert.Single(projection.AiMessages);
        Assert.Equal("message-release-1", message.ArtifactId);
        Assert.Equal("我先看核心承接，不急着追。", message.Text);
        Assert.DoesNotContain("raw internal fallback", message.Text, StringComparison.Ordinal);
    }

    [Fact]
    public void ProjectionRejectsAnUnknownNewAiMessageKind()
    {
        var payload = """{"contract":"companion-client-event/v1","event_id":"projection","cycle_id":"c1","type":"projection.ready","created_at":"2026-08-25T01:00:00Z","payload":{"cycle":{"task_key":"conversation.daily"},"ai_messages":[{"artifact_id":"a1","kind":"future_internal_stage","at":"2026-08-25T01:00:00Z","text":"must stay hidden"}]}}""";

        var projection = CompanionEventProjection.Project([payload]);

        Assert.NotNull(projection);
        Assert.Empty(projection.AiMessages);
    }

    [Fact]
    public void ProjectionPrefersTheVersionedPublishedMessageOverLegacyText()
    {
        var events = new[]
        {
            """{"contract":"companion-client-event/v1","event_id":"projection","cycle_id":"c1","type":"projection.ready","created_at":"2026-09-01T01:02:00Z","payload":{"cycle":{"task_key":"conversation.daily","state":"open"},"ai_messages":[{"artifact_id":"message-1","kind":"ai_chat","at":"2026-09-01T01:00:00Z","text":"raw internal report","message":{"contract":"companion-published-message/v2","message_id":"message-1","sealed_at":"2026-09-01T01:00:00Z","kind":"ai_chat","parts":[{"kind":"speech","text":"我倾向于先等承接确认。"},{"kind":"material","markdown":"> 公告原文","material_id":"notice-1","source_title":"公告","source_url":"https://example.com/notice"}],"text_projection":"我倾向于先等承接确认。"}}]}}"""
        };

        var projection = CompanionEventProjection.Project(events)!;

        Assert.Equal("我倾向于先等承接确认。", Assert.Single(projection.AiMessages).Text);
        Assert.Equal(["speech", "material"], projection.AiMessages[0].Parts!.Select(part => part.Kind));
        Assert.Equal("notice-1", projection.AiMessages[0].Parts![1].MaterialId);
        Assert.Equal("公告", projection.AiMessages[0].Parts![1].SourceTitle);
    }

    [Fact]
    public void SelectsTheNewestValidCompanionCycleId()
    {
        var events = new[]
        {
            """{"contract":"companion-client-event/v1","event_id":"new","cycle_id":"cycle-new","type":"m0.ready","created_at":"2026-08-25T01:01:00Z","payload":{}}""",
            """{"contract":"companion-client-event/v1","event_id":"old","cycle_id":"cycle-old","type":"cycle.created","created_at":"2026-08-25T01:00:00Z","payload":{}}"""
        };

        Assert.True(CompanionEventProjection.TryGetLatestCycleId(events, out var cycleId));
        Assert.Equal("cycle-new", cycleId);
    }

    [Fact]
    public void ProjectsEveryManualCycleByCycleIdentity()
    {
        var events = new[]
        {
            """{"contract":"companion-client-event/v1","event_id":"manual-1","cycle_id":"manual-1","type":"analysis.request.created","created_at":"2026-08-29T02:00:00Z","payload":{"cycle":{"task_key":"manual.non_trading_outlook","state":"queued","scheduled_for":"2026-08-29T10:00:00.000001+08:00","trigger":"manual_chat","requested_at":"2026-08-29T10:00:00+08:00","task_profile_id":"non_trading_outlook","task_profile_json":"{\"display_name\":\"非交易日市场环境总结与下一交易日预判\"}"}}}""",
            """{"contract":"companion-client-event/v1","event_id":"manual-2","cycle_id":"manual-2","type":"analysis.request.created","created_at":"2026-08-29T02:01:00Z","payload":{"cycle":{"task_key":"manual.non_trading_outlook","state":"queued","scheduled_for":"2026-08-29T10:01:00.000001+08:00","trigger":"manual_chat","requested_at":"2026-08-29T10:01:00+08:00","task_profile_id":"non_trading_outlook","task_profile_json":"{\"display_name\":\"非交易日市场环境总结与下一交易日预判\"}"}}}"""
        };

        var projections = CompanionEventProjection.ProjectAll(events);

        Assert.Equal(["manual-1", "manual-2"], projections.Select(item => item.CycleId));
        Assert.All(projections, item => Assert.Equal("manual_chat", item.Trigger));
        Assert.All(projections, item => Assert.Equal("manual.non_trading_outlook", item.TaskKey));
        Assert.All(projections, item => Assert.Equal("非交易日市场环境总结与下一交易日预判", item.TaskProfileDisplayName));
        Assert.Equal(DateTimeOffset.Parse("2026-08-29T10:00:00+08:00", CultureInfo.InvariantCulture, DateTimeStyles.RoundtripKind), projections[0].RequestedAt);
    }

    [Fact]
    public void ProjectsDismissedManualCycleForImmediateRemovalFromTodayList()
    {
        var events = new[]
        {
            """{"contract":"companion-client-event/v1","event_id":"manual","cycle_id":"manual-1","type":"analysis.request.created","created_at":"2026-08-29T02:00:00Z","payload":{"cycle":{"task_key":"daily.execution.0945","state":"failed","scheduled_for":"2026-08-29T13:00:00.000001+08:00","trigger":"manual_chat","requested_at":"2026-08-29T13:00:00+08:00","task_profile_id":"intraday_execution"}}}""",
            """{"contract":"companion-client-event/v1","event_id":"dismissed","cycle_id":"manual-1","type":"analysis.dismissed","created_at":"2026-08-29T03:00:00Z","payload":{"cycle":{"task_key":"daily.execution.0945","state":"failed","trigger":"manual_chat","requested_at":"2026-08-29T13:00:00+08:00","task_profile_id":"intraday_execution"}}}"""
        };

        var projection = Assert.Single(CompanionEventProjection.ProjectAll(events));

        Assert.True(projection.IsDismissed);
    }

    [Fact]
    public void IgnoresMalformedAndOtherContractEvents()
    {
        var events = new[] { "not-json", """{"contract":"other/v1","cycle_id":"wrong"}""" };
        Assert.False(CompanionEventProjection.TryGetLatestCycleId(events, out _));
    }

    [Fact]
    public void ProjectsTaskScopedAiTimelineAndFrozenH0()
    {
        var events = new[]
        {
            """{"contract":"companion-client-event/v1","event_id":"start","cycle_id":"cycle-1","type":"m0.started","created_at":"2026-08-25T00:59:00Z","payload":{"cycle":{"task_key":"daily.execution.0945","state":"researching_m0","scheduled_for":"2026-08-25T09:45:00+08:00"}}}""",
            """{"contract":"companion-client-event/v1","event_id":"m0","cycle_id":"cycle-1","type":"m0.ready","created_at":"2026-08-25T01:00:00Z","payload":{"source_artifact_id":"a0","m0":"今天先看客观信息。","h0_auto_submit_at":"2026-08-25T02:20:00Z","m1_publish_deadline":"2026-08-25T02:30:00Z","cycle":{"task_key":"daily.execution.0945","state":"awaiting_h0"}}}""",
            """{"contract":"companion-client-event/v1","event_id":"h0","cycle_id":"cycle-1","type":"h0.locked","created_at":"2026-08-25T01:10:00Z","payload":{"has_h0":true,"messages":[{"message_id":"u1","body_text":"我认为承接很弱","state":"submitted","phase":"h0","submitted_at":"2026-08-25T01:10:00Z"}],"cycle":{"task_key":"daily.execution.0945","state":"researching_m1","h0_locked_at":"2026-08-25T01:10:00Z"}}}""",
            """{"contract":"companion-client-event/v1","event_id":"m1","cycle_id":"cycle-1","type":"m1.ready","created_at":"2026-08-25T01:12:00Z","payload":{"source_artifact_id":"a1","m1":"我的独立判断偏谨慎。","cycle":{"task_key":"daily.execution.0945","state":"synthesizing_m2"}}}""",
            """{"contract":"companion-client-event/v1","event_id":"m2","cycle_id":"cycle-1","type":"m2.ready","created_at":"2026-08-25T01:15:00Z","payload":{"source_artifact_id":"a2","m2":"综合后仍以等待为主。","cycle":{"task_key":"daily.execution.0945","state":"complete"}}}"""
        };

        var projection = CompanionEventProjection.ProjectForTask(events, "daily.execution.0945");

        Assert.NotNull(projection);
        Assert.Equal("cycle-1", projection.CycleId);
        Assert.True(projection.IsH0Locked);
        Assert.Equal(3, projection.AiMessages.Count);
        Assert.Equal(["m0", "m1", "m2"], projection.AiMessages.Select(message => message.Kind));
        Assert.Equal(DateTimeOffset.Parse("2026-08-25T01:10:00Z", CultureInfo.InvariantCulture, DateTimeStyles.RoundtripKind), projection.AiMessages[1].StartedAt);
        Assert.Equal(DateTimeOffset.Parse("2026-08-25T01:12:00Z", CultureInfo.InvariantCulture, DateTimeStyles.RoundtripKind), projection.AiMessages[1].CompletedAt);
        Assert.Single(projection.UserMessages);
        Assert.True(projection.UserMessages[0].CountsForM1);
        Assert.Equal("我认为承接很弱", projection.UserMessages[0].Text);
        Assert.Equal(DateTimeOffset.Parse("2026-08-25T02:20:00Z", CultureInfo.InvariantCulture, DateTimeStyles.RoundtripKind), projection.H0AutoSubmitAt);
    }

    [Fact]
    public void AiStageActionsUseOnePendingBubbleUntilTheFormalMessageArrives()
    {
        var m0Started =
            """{"contract":"companion-client-event/v1","event_id":"m0-start","cycle_id":"cycle-1","type":"m0.started","created_at":"2026-09-01T07:20:13Z","payload":{"cycle":{"task_key":"daily.review.1520","state":"researching_m0"}}}""";
        var retrying =
            """{"contract":"companion-client-event/v1","event_id":"retry","cycle_id":"cycle-1","type":"research.retrying","created_at":"2026-09-01T07:25:08Z","payload":{"reason":"上游暂时不可用，正在自动重试。","cycle":{"task_key":"daily.review.1520","state":"researching_m0"}}}""";
        var m0Ready =
            """{"contract":"companion-client-event/v1","event_id":"m0-ready","cycle_id":"cycle-1","type":"m0.ready","created_at":"2026-09-01T07:26:00Z","payload":{"source_artifact_id":"m0","m0":"收盘复盘事实。","cycle":{"task_key":"daily.review.1520","state":"awaiting_h0"}}}""";
        var m1Started =
            """{"contract":"companion-client-event/v1","event_id":"m1-start","cycle_id":"cycle-1","type":"m1.started","created_at":"2026-09-01T07:27:00Z","payload":{"cycle":{"task_key":"daily.review.1520","state":"researching_m1"}}}""";
        var m1Ready =
            """{"contract":"companion-client-event/v1","event_id":"m1-ready","cycle_id":"cycle-1","type":"m1.ready","created_at":"2026-09-01T07:28:00Z","payload":{"source_artifact_id":"m1","m1":"独立判断。","cycle":{"task_key":"daily.review.1520","state":"synthesizing_m2"}}}""";
        var m2Started =
            """{"contract":"companion-client-event/v1","event_id":"m2-start","cycle_id":"cycle-1","type":"m2.started","created_at":"2026-09-01T07:28:01Z","payload":{"cycle":{"task_key":"daily.review.1520","state":"synthesizing_m2"}}}""";

        var researching = CompanionEventProjection.ProjectForCycle([m0Started], "cycle-1")!;
        Assert.Equal("action_pending", Assert.Single(researching.AiMessages).Kind);
        Assert.Equal("AI 正在研究中", researching.AiMessages[0].Text);

        var retried = CompanionEventProjection.ProjectForCycle([m0Started, retrying], "cycle-1")!;
        Assert.Equal("我正在重新核对，稍等一下。", Assert.Single(retried.AiMessages).Text);

        var judging = CompanionEventProjection.ProjectForCycle([m0Started, retrying, m0Ready, m1Started], "cycle-1")!;
        Assert.Equal(["m0", "action_pending"], judging.AiMessages.Select(message => message.Kind));
        Assert.Equal("AI 正在形成独立判断", judging.AiMessages[1].Text);

        var synthesizing = CompanionEventProjection.ProjectForCycle(
            [m0Started, retrying, m0Ready, m1Started, m1Ready, m2Started], "cycle-1")!;
        Assert.Equal(["m0", "m1", "action_pending"], synthesizing.AiMessages.Select(message => message.Kind));
        Assert.Equal("AI 正在综合判断", synthesizing.AiMessages[2].Text);
    }

    [Fact]
    public void ProjectsStagedThenWithdrawnMessageWithoutTreatingItAsH0()
    {
        var events = new[]
        {
            """{"contract":"companion-client-event/v1","event_id":"m0","cycle_id":"c1","type":"m0.ready","created_at":"2026-08-25T01:00:00Z","payload":{"m0":"M0","cycle":{"task_key":"daily.execution.0945","state":"awaiting_h0"}}}""",
            """{"contract":"companion-client-event/v1","event_id":"stage","cycle_id":"c1","type":"message.staged","created_at":"2026-08-25T01:01:00Z","payload":{"message":{"message_id":"u1","body_text":"还没提交","state":"staged","phase":"h0","staged_at":"2026-08-25T01:01:00Z"},"cycle":{"task_key":"daily.execution.0945","state":"awaiting_h0"}}}""",
            """{"contract":"companion-client-event/v1","event_id":"withdraw","cycle_id":"c1","type":"message.withdrawn","created_at":"2026-08-25T01:02:00Z","payload":{"message_id":"u1","cycle":{"task_key":"daily.execution.0945","state":"awaiting_h0"}}}"""
        };

        var projection = CompanionEventProjection.Project(events);

        Assert.NotNull(projection);
        Assert.Empty(projection.UserMessages);
        Assert.False(projection.IsH0Locked);
    }

    [Fact]
    public void ProjectionReadyRestoresMessagesAfterClientRestart()
    {
        var events = new[]
        {
            """{"contract":"companion-client-event/v1","event_id":"projection","cycle_id":"c1","type":"projection.ready","created_at":"2026-08-25T01:02:00Z","payload":{"cycle":{"task_key":"daily.execution.0945","state":"awaiting_h0","h0_auto_submit_at":"2026-08-25T02:20:00Z"},"ai_messages":[{"artifact_id":"a0","kind":"m0","at":"2026-08-25T01:00:00Z","text":"M0"}],"user_messages":[{"message_id":"u1","state":"staged","phase":"h0","text":"待提交","at":"2026-08-25T01:01:00Z"}]}}"""
        };

        var projection = CompanionEventProjection.Project(events);

        Assert.NotNull(projection);
        Assert.Single(projection.AiMessages);
        Assert.Single(projection.UserMessages);
        Assert.Equal("staged", projection.UserMessages[0].State);
    }

    [Fact]
    public void StreamingReplyShowsPendingNoticeUntilTheFirstVisibleTextArrives()
    {
        var started = """{"contract":"companion-client-event/v1","event_id":"started","cycle_id":"c1","type":"chat.stream.started","created_at":"2026-08-25T01:02:00Z","payload":{"cycle":{"task_key":"conversation.daily","state":"open"},"stream":{"stream_id":"s1","state":"streaming","created_at":"2026-08-25T01:02:00Z"}}}""";
        var delta = """{"contract":"companion-client-event/v1","event_id":"delta","cycle_id":"c1","type":"chat.stream.delta","created_at":"2026-08-25T01:02:01Z","payload":{"cycle":{"task_key":"conversation.daily","state":"open"},"stream_id":"s1","text":"我先核对一下。"}}""";

        var pending = CompanionEventProjection.Project([started]);
        var replying = CompanionEventProjection.Project([started, delta]);

        Assert.NotNull(pending);
        Assert.Empty(pending.AiMessages);
        Assert.True(pending.IsCompanionThinking);
        Assert.NotNull(replying);
        Assert.Equal("chat", Assert.Single(replying.AiMessages).Kind);
        Assert.Equal("我先核对一下。", replying.AiMessages[0].Text);
        Assert.False(replying.IsCompanionThinking);
    }

    [Fact]
    public void ProjectionReadyRestoresAnEmptyActiveReplyAsPending()
    {
        var events = new[]
        {
            """{"contract":"companion-client-event/v1","event_id":"projection","cycle_id":"c1","type":"projection.ready","created_at":"2026-08-25T01:02:00Z","payload":{"cycle":{"task_key":"conversation.daily","state":"open"},"stream_messages":[{"stream_id":"s1","state":"streaming","text":"","created_at":"2026-08-25T01:01:59Z"}]}}"""
        };

        var projection = CompanionEventProjection.Project(events);

        Assert.NotNull(projection);
        Assert.Empty(projection.AiMessages);
        Assert.True(projection.IsCompanionThinking);
    }

    [Fact]
    public void PreM0MessagesBecomeSubmittedContextWithoutCountingAsH0()
    {
        var events = new[]
        {
            """{"contract":"companion-client-event/v1","event_id":"created","cycle_id":"pre-1","type":"cycle.created","created_at":"2026-08-25T23:10:00Z","payload":{"cycle_id":"pre-1","task_key":"daily.opportunity.0900","state":"queued","scheduled_for":"2026-08-26T09:00:00+08:00"}}""",
            """{"contract":"companion-client-event/v1","event_id":"staged","cycle_id":"pre-1","type":"message.staged","created_at":"2026-08-25T23:11:00Z","payload":{"cycle":{"task_key":"daily.opportunity.0900","state":"queued"},"message":{"message_id":"pre-message","body_text":"重点看看机器人消息源","state":"staged","phase":"pre_m0","staged_at":"2026-08-25T23:11:00Z"}}}""",
            """{"contract":"companion-client-event/v1","event_id":"submitted","cycle_id":"pre-1","type":"pre_m0.submitted","created_at":"2026-08-25T23:12:00Z","payload":{"cycle":{"task_key":"daily.opportunity.0900","state":"queued"},"messages":[{"message_id":"pre-message","body_text":"重点看看机器人消息源","state":"submitted","phase":"pre_m0","submitted_at":"2026-08-25T23:12:00Z"}]}}""",
            """{"contract":"companion-client-event/v1","event_id":"reply","cycle_id":"pre-1","type":"premarket.reply.ready","created_at":"2026-08-25T23:13:00Z","payload":{"cycle":{"task_key":"daily.opportunity.0900","state":"queued"},"text":"我先记下，等会儿会独立核实传播源头。","source_artifact_id":"pre-ai-1"}}""",
            """{"contract":"companion-client-event/v1","event_id":"locked","cycle_id":"pre-1","type":"pre_m0.locked","created_at":"2026-08-26T00:30:00Z","payload":{"cycle":{"task_key":"daily.opportunity.0900","state":"queued"},"messages":[{"message_id":"pre-message","body_text":"重点看看机器人消息源","state":"submitted","phase":"pre_m0","submitted_at":"2026-08-26T00:30:00Z"}]}}""",
        };

        var projection = CompanionEventProjection.Project(events);

        Assert.NotNull(projection);
        var message = Assert.Single(projection.UserMessages);
        Assert.Equal("submitted", message.State);
        Assert.Equal("pre_m0", message.Phase);
        Assert.False(message.CountsForM1);
        var reply = Assert.Single(projection.AiMessages);
        Assert.Equal("premarket", reply.Kind);
        Assert.Contains("独立核实", reply.Text);
    }

    [Fact]
    public void ProjectsMissedReasonAsNaturalVisibleFault()
    {
        var events = new[]
        {
            """{"contract":"companion-client-event/v1","event_id":"missed","cycle_id":"cycle-1","type":"cycle.missed","created_at":"2026-08-25T03:00:00Z","payload":{"reason":"服务恢复时已超过补偿窗口","cycle":{"task_key":"daily.execution.1030","state":"missed"}}}"""
        };

        var projection = CompanionEventProjection.ProjectForTask(events, "daily.execution.1030");

        Assert.NotNull(projection);
        Assert.Equal("missed", projection.State);
        Assert.Equal("服务恢复时已超过补偿窗口", projection.ErrorText);
        Assert.Equal("fault", projection.AiMessages[0].Kind);
    }

    [Fact]
    public void SuccessfulM1ClearsEarlierTechnicalFaultCards()
    {
        var events = new[]
        {
            """{"contract":"companion-client-event/v1","event_id":"fault","cycle_id":"cycle-1","type":"m1.failed","created_at":"2026-08-25T03:00:00Z","payload":{"reason":"M1 因输出格式配置错误中断。","cycle":{"task_key":"daily.review.1520","state":"m1_retry_wait"}}}""",
            """{"contract":"companion-client-event/v1","event_id":"ready","cycle_id":"cycle-1","type":"m1.ready","created_at":"2026-08-25T03:02:00Z","payload":{"m1":"修复后的独立判断。","cycle":{"task_key":"daily.review.1520","state":"complete"}}}"""
        };

        var projection = CompanionEventProjection.ProjectForTask(events, "daily.review.1520");

        Assert.NotNull(projection);
        Assert.Null(projection.ErrorText);
        Assert.Single(projection.AiMessages);
        Assert.Equal("m1", projection.AiMessages[0].Kind);
    }

    [Fact]
    public void ConsecutiveFaultsShareOneCardAndRepeatedTextUsesLatestOccurrence()
    {
        const string repeated = "M1 遇到技术故障，未能完成。详细诊断已保留在本地审计记录中。";
        var events = new[]
        {
            """{"contract":"companion-client-event/v1","event_id":"fault-1","cycle_id":"cycle-1","type":"m1.failed","created_at":"2026-08-31T07:48:43Z","payload":{"reason":"__REASON__","cycle":{"task_key":"daily.review.1520","state":"m1_retry_wait"}}}""".Replace("__REASON__", repeated, StringComparison.Ordinal),
            """{"contract":"companion-client-event/v1","event_id":"fault-2","cycle_id":"cycle-1","type":"m1.failed","created_at":"2026-08-31T07:49:19Z","payload":{"reason":"__REASON__","cycle":{"task_key":"daily.review.1520","state":"m1_retry_wait"}}}""".Replace("__REASON__", repeated, StringComparison.Ordinal),
            """{"contract":"companion-client-event/v1","event_id":"fault-other","cycle_id":"cycle-1","type":"m2.deferred","created_at":"2026-08-31T07:50:00Z","payload":{"reason":"M2 已延后。","cycle":{"task_key":"daily.review.1520","state":"m2_deferred"}}}"""
        };

        var projection = CompanionEventProjection.ProjectForTask(events, "daily.review.1520");

        Assert.NotNull(projection);
        var fault = Assert.Single(projection.AiMessages);
        Assert.Equal("fault", fault.Kind);
        Assert.Equal(1, fault.Text.Split(repeated, StringSplitOptions.None).Length - 1);
        Assert.Contains("M2 已延后。", fault.Text, StringComparison.Ordinal);
        Assert.Equal(DateTimeOffset.Parse("2026-08-31T07:50:00Z", CultureInfo.InvariantCulture, DateTimeStyles.RoundtripKind), fault.At);
    }

    [Fact]
    public void OutcomeAndReflectionStayInTheCurrentTaskTimeline()
    {
        var events = new[]
        {
            """{"contract":"companion-client-event/v1","event_id":"outcome","cycle_id":"cycle-1","type":"outcome.ready","created_at":"2026-08-26T08:10:00Z","payload":{"source_artifact_id":"o1","text":"昨天的看多判断已被验证为错误。","cycle":{"task_key":"daily.execution.0945","state":"complete"}}}""",
            """{"contract":"companion-client-event/v1","event_id":"reflection","cycle_id":"cycle-1","type":"reflection.ready","created_at":"2026-08-26T08:11:00Z","payload":{"source_artifact_id":"r1","text":"我会把这次反证带进以后相似判断。","cycle":{"task_key":"daily.execution.0945","state":"complete"}}}"""
        };

        var projection = CompanionEventProjection.ProjectForTask(events, "daily.execution.0945");

        Assert.NotNull(projection);
        Assert.Equal(["outcome", "reflection"], projection.AiMessages.Select(message => message.Kind));
    }
}
