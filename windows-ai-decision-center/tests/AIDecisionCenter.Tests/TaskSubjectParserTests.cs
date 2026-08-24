using AIDecisionCenter.Core.Parsing;

namespace AIDecisionCenter.Tests;

public sealed class TaskSubjectParserTests
{
    [Fact]
    public void ParsesCanonicalSubject()
    {
        var success = TaskSubjectParser.TryParse(
            "[ChatGPTTask][A股][14:30] 操作决策 2026-08-24",
            out var parsed);

        Assert.True(success);
        Assert.NotNull(parsed);
        Assert.Equal("A股", parsed.Project);
        Assert.Equal(new TimeOnly(14, 30), parsed.Slot);
        Assert.Equal("操作决策", parsed.TaskType);
        Assert.Equal(new DateOnly(2026, 8, 24), parsed.ScheduledDate);
    }

    [Theory]
    [InlineData("ChatGPTTask A股 14:30")]
    [InlineData("[ChatGPTTask][A股][25:30] 操作决策 2026-08-24")]
    [InlineData("[ChatGPTTask][A股][14:30] 操作决策 2026-13-24")]
    public void RejectsNonCanonicalSubjects(string subject)
    {
        Assert.False(TaskSubjectParser.TryParse(subject, out _));
    }
}
