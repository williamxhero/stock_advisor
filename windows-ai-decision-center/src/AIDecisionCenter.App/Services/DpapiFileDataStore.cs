using System.Security.Cryptography;
using System.Text.Json;
using Google.Apis.Util.Store;

namespace AIDecisionCenter.App.Services;

public sealed class DpapiFileDataStore : IDataStore
{
    private static readonly byte[] Entropy = "AIDecisionCenter.GoogleOAuth.v1"u8.ToArray();
    private readonly string _directory;

    public DpapiFileDataStore(string directory)
    {
        _directory = directory;
        Directory.CreateDirectory(_directory);
    }

    public Task ClearAsync()
    {
        foreach (var file in Directory.EnumerateFiles(_directory, "*.token"))
        {
            File.Delete(file);
        }

        return Task.CompletedTask;
    }

    public Task DeleteAsync<T>(string key)
    {
        var path = GetPath(key);
        if (File.Exists(path))
        {
            File.Delete(path);
        }

        return Task.CompletedTask;
    }

    public async Task<T?> GetAsync<T>(string key)
    {
        var path = GetPath(key);
        if (!File.Exists(path))
        {
            return default;
        }

        var encrypted = await File.ReadAllBytesAsync(path).ConfigureAwait(false);
        var json = ProtectedData.Unprotect(encrypted, Entropy, DataProtectionScope.CurrentUser);
        return JsonSerializer.Deserialize<T>(json);
    }

    public async Task StoreAsync<T>(string key, T value)
    {
        var json = JsonSerializer.SerializeToUtf8Bytes(value);
        var encrypted = ProtectedData.Protect(json, Entropy, DataProtectionScope.CurrentUser);
        await File.WriteAllBytesAsync(GetPath(key), encrypted).ConfigureAwait(false);
    }

    private string GetPath(string key)
    {
        var safeKey = Convert.ToHexString(SHA256.HashData(System.Text.Encoding.UTF8.GetBytes(key)));
        return Path.Combine(_directory, $"{safeKey}.token");
    }
}
