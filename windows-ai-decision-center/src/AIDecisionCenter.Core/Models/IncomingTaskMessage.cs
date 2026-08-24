namespace AIDecisionCenter.Core.Models;

public sealed record IncomingTaskMessage(
    string ExternalId,
    string Subject,
    string BodyMarkdown,
    DateTimeOffset ReceivedAt);
