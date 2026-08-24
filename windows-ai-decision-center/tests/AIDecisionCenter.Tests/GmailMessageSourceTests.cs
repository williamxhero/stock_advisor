using AIDecisionCenter.App.Services;
namespace AIDecisionCenter.Tests;

public sealed class GmailMessageSourceTests
{
    [Fact]
    public void GoogleApiApplicationNameProducesAValidUserAgent()
    {
        using var request = new HttpRequestMessage();
        var userAgent = $"{GmailMessageSource.GoogleApiApplicationName} google-api-dotnet-client/1.75.0.0 (gzip)";

        var exception = Record.Exception(() => request.Headers.UserAgent.ParseAdd(userAgent));

        Assert.Null(exception);
    }
}
