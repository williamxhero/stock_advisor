using System.Text.Json;
using Google.Apis.Auth.OAuth2;

namespace AIDecisionCenter.App.Services;

public static class OAuthClientLoader
{
    public static ClientSecrets Load(string path)
    {
        if (!File.Exists(path))
        {
            throw new FileNotFoundException("请先把 Google OAuth Desktop Client JSON 放到配置目录。", path);
        }

        using var document = JsonDocument.Parse(File.ReadAllText(path));
        var root = document.RootElement;
        if (root.TryGetProperty("web", out _))
        {
            throw new InvalidDataException(
                "当前 OAuth JSON 是 Web application 类型，不能用于本 Windows 客户端。" +
                "请在 Google Cloud Console 创建 Desktop app 类型的 OAuth Client 并重新下载 JSON。");
        }

        if (!root.TryGetProperty("installed", out var client))
        {
            throw new InvalidDataException(
                "OAuth JSON 不是 Google Desktop app 客户端配置：缺少 installed 节点。");
        }

        var clientId = client.GetProperty("client_id").GetString();
        var clientSecret = client.GetProperty("client_secret").GetString();
        if (string.IsNullOrWhiteSpace(clientId) || string.IsNullOrWhiteSpace(clientSecret))
        {
            throw new InvalidDataException("OAuth JSON 缺少 client_id 或 client_secret。");
        }

        return new ClientSecrets { ClientId = clientId, ClientSecret = clientSecret };
    }
}
