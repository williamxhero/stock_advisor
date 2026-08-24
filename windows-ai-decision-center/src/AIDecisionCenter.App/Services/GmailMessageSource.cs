using System.Text;
using System.Text.Json;
using AIDecisionCenter.Core.Models;
using AIDecisionCenter.Core.Parsing;
using Google.Apis.Auth.OAuth2;
using Google.Apis.Gmail.v1;
using Google.Apis.Gmail.v1.Data;
using Google.Apis.Services;

namespace AIDecisionCenter.App.Services;

public sealed class GmailMessageSource : IGmailMessageSource, IDisposable
{
    internal const string GoogleApiApplicationName = "AIDecisionCenter";

    private readonly AppPaths _paths;
    private GmailService? _service;

    public GmailMessageSource(AppPaths paths)
    {
        _paths = paths;
    }

    public bool IsConfigured => ConfigurationError is null;

    public bool IsAuthorized => Directory.Exists(_paths.TokenDirectory) &&
        Directory.EnumerateFiles(_paths.TokenDirectory, "*.token").Any();

    public string? ConfigurationError
    {
        get
        {
            try
            {
                _ = OAuthClientLoader.Load(_paths.OAuthClientPath);
                return null;
            }
            catch (Exception exception) when (exception is IOException or InvalidDataException or JsonException or KeyNotFoundException)
            {
                return exception.Message;
            }
        }
    }

    public async Task<IReadOnlyList<IncomingTaskMessage>> FetchAsync(
        AppSettings settings,
        IReadOnlySet<string> knownExternalIds,
        CancellationToken cancellationToken = default)
    {
        var service = await GetServiceAsync(cancellationToken).ConfigureAwait(false);
        var idsToFetch = new List<string>();
        var seenIds = new HashSet<string>(knownExternalIds, StringComparer.Ordinal);
        string? pageToken = null;
        do
        {
            var listRequest = service.Users.Messages.List("me");
            listRequest.Q = settings.Gmail.Query;
            listRequest.MaxResults = Math.Clamp(settings.Gmail.MaxMessagesPerSync, 1, 500);
            listRequest.PageToken = pageToken;
            var listResponse = await listRequest.ExecuteAsync(cancellationToken).ConfigureAwait(false);
            foreach (var summary in listResponse.Messages ?? [])
            {
                if (!string.IsNullOrWhiteSpace(summary.Id) && seenIds.Add(summary.Id))
                {
                    idsToFetch.Add(summary.Id);
                }
            }

            pageToken = listResponse.NextPageToken;
        }
        while (!string.IsNullOrWhiteSpace(pageToken));

        var messages = new List<IncomingTaskMessage>(idsToFetch.Count);
        foreach (var messageId in idsToFetch)
        {
            var getRequest = service.Users.Messages.Get("me", messageId);
            getRequest.Format = UsersResource.MessagesResource.GetRequest.FormatEnum.Full;
            var message = await getRequest.ExecuteAsync(cancellationToken).ConfigureAwait(false);
            var subject = GetHeader(message.Payload, "Subject");
            if (string.IsNullOrWhiteSpace(subject))
            {
                continue;
            }

            var body = ExtractBody(message.Payload);
            var receivedAt = message.InternalDate is long milliseconds
                ? DateTimeOffset.FromUnixTimeMilliseconds(milliseconds)
                : DateTimeOffset.UtcNow;
            messages.Add(new IncomingTaskMessage(message.Id, subject, body, receivedAt));
        }

        return messages;
    }

    public void Dispose()
    {
        _service?.Dispose();
    }

    private async Task<GmailService> GetServiceAsync(CancellationToken cancellationToken)
    {
        if (_service is not null)
        {
            return _service;
        }

        var secrets = OAuthClientLoader.Load(_paths.OAuthClientPath);
        var credential = await GoogleWebAuthorizationBroker.AuthorizeAsync(
            secrets,
            [GmailService.Scope.GmailReadonly],
            "default-user",
            cancellationToken,
            new DpapiFileDataStore(_paths.TokenDirectory)).ConfigureAwait(false);

        _service = new GmailService(new BaseClientService.Initializer
        {
            HttpClientInitializer = credential,
            ApplicationName = GoogleApiApplicationName
        });
        return _service;
    }

    private static string GetHeader(MessagePart? payload, string name)
    {
        return payload?.Headers?.FirstOrDefault(header =>
            string.Equals(header.Name, name, StringComparison.OrdinalIgnoreCase))?.Value ?? string.Empty;
    }

    private static string ExtractBody(MessagePart? part)
    {
        if (part is null)
        {
            return string.Empty;
        }

        var plain = FindBody(part, "text/plain");
        if (!string.IsNullOrWhiteSpace(plain))
        {
            return plain.Trim();
        }

        var html = FindBody(part, "text/html");
        return string.IsNullOrWhiteSpace(html) ? string.Empty : PlainTextSanitizer.FromHtml(html);
    }

    private static string? FindBody(MessagePart part, string mimeType)
    {
        if (string.Equals(part.MimeType, mimeType, StringComparison.OrdinalIgnoreCase) &&
            !string.IsNullOrWhiteSpace(part.Body?.Data))
        {
            return DecodeBase64Url(part.Body.Data);
        }

        if (part.Parts is null)
        {
            return null;
        }

        foreach (var child in part.Parts)
        {
            var body = FindBody(child, mimeType);
            if (body is not null)
            {
                return body;
            }
        }

        return null;
    }

    private static string DecodeBase64Url(string data)
    {
        var normalized = data.Replace('-', '+').Replace('_', '/');
        normalized = normalized.PadRight(normalized.Length + ((4 - (normalized.Length % 4)) % 4), '=');
        return Encoding.UTF8.GetString(Convert.FromBase64String(normalized));
    }
}
