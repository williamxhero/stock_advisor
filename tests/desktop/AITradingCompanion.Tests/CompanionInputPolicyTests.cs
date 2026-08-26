using AITradingCompanion.Desktop.Services;

namespace AITradingCompanion.Tests;

public sealed class CompanionInputPolicyTests
{
    [Theory]
    [InlineData("queued", true)]
    [InlineData("awaiting_h0", true)]
    [InlineData("researching_m0", false)]
    [InlineData(null, false)]
    public void DraftingAvailabilityFollowsTheCyclePhase(string? state, bool expected)
    {
        Assert.Equal(expected, CompanionInputPolicy.CanDraft(state));
    }

    [Fact]
    public void QueuedMessagesArePreM0AndCanBeSubmittedWithoutBecomingH0()
    {
        Assert.Equal("pre_m0", CompanionInputPolicy.MessagePhase("queued", h0Locked: false));
        Assert.True(CompanionInputPolicy.CanCommit("queued", h0Locked: false, stagedMessages: 1));
        Assert.False(CompanionInputPolicy.CanCommit("queued", h0Locked: false, stagedMessages: 0));
        Assert.Equal("提交", CompanionInputPolicy.CommitLabel("queued", h0Locked: false));
        Assert.True(CompanionInputPolicy.CanCommit("awaiting_h0", h0Locked: false, stagedMessages: 0));
        Assert.Equal("提交 H0", CompanionInputPolicy.CommitLabel("awaiting_h0", h0Locked: false));
    }
}
