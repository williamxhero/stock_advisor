namespace AIDecisionCenter.Core.Models;

public sealed record IncomingTaskMessage(
    string ExternalId,
    string Source,
    string SourceRunId,
    string Project,
    string TaskKey,
    string TaskType,
    DateTimeOffset ScheduledFor,
    DateTimeOffset CompletedAt,
    DateTimeOffset ReceivedAt,
    TaskMessageStatus Status,
    string RegistryId,
    string ProtocolId,
    string Summary,
    string BodyMarkdown,
    string PayloadJson,
    string ContentSha256);
