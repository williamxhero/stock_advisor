namespace AITradingCompanion.Desktop.Services;

public static class CompanionInputPolicy
{
    public static bool CanDraft(string? state) => state is
        "queued" or "awaiting_h0" or "voice_grace" or "researching_m1" or "judging_m1" or
        "m1_retry_wait" or "synthesizing_m2" or "m2_deferred" or "complete";

    public static string MessagePhase(string? state, bool h0Locked) =>
        state == "queued" ? "pre_m0" : h0Locked ? "chat" : "h0";

    public static bool CanCommit(string? state, bool h0Locked, int stagedMessages) =>
        CanDraft(state) && (state == "queued" ? stagedMessages > 0 : !h0Locked || stagedMessages > 0);

    public static string CommitLabel(string? state, bool h0Locked) =>
        state == "queued" || h0Locked ? "提交" : "提交 H0";
}
