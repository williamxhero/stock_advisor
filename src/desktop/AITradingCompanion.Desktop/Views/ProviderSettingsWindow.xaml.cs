using System.Text.Json;
using System.Text.Json.Nodes;
using System.Windows;
using System.Windows.Controls;
using AITradingCompanion.Desktop.Services;
using MessageBox = System.Windows.MessageBox;
using TextBox = System.Windows.Controls.TextBox;

namespace AITradingCompanion.Desktop.Views;

public partial class ProviderSettingsWindow : Window
{
    private readonly AppPaths _paths;
    private readonly CompanionExchangeService _exchange;
    private JsonObject _root = new();

    public ProviderSettingsWindow(AppPaths paths, CompanionExchangeService exchange)
    {
        InitializeComponent();
        _paths = paths; _exchange = exchange;
        LoadSettings();
    }

    private void LoadSettings()
    {
        if (File.Exists(_paths.RuntimeSettingsPath))
            _root = JsonNode.Parse(File.ReadAllText(_paths.RuntimeSettingsPath)) as JsonObject ?? new JsonObject();
        var provider = Object("provider");
        var models = Object(provider, "models");
        ProviderUrl.Text = String(provider, "base_url", "http://yosef-server:8317/v1");
        CredentialTarget.Text = String(provider, "credential_target", "AITradingCompanion/CPA");
        SetModel(models, "research", ResearchModel, ResearchEffort, "gpt-5.6-terra");
        SetModel(models, "judgment", JudgmentModel, JudgmentEffort, "gpt-5.6-sol");
        SetModel(models, "fast", FastModel, FastEffort, "gpt-5.6-terra");
        var research = Object("research");
        SearxUrl.Text = String(Object(research, "searxng"), "base_url", "http://yosef-server:8801");
        var browser = Object(research, "playwright");
        EdgeProfile.Text = String(browser, "edge_profile", "Profile 2");
        BrowserProfileDirectory.Text = String(browser, "profile_directory", "browser-profile");
        DownloadLimit.Text = String(browser, "download_limit_mb", "50");
    }

    private async void Save_Click(object sender, RoutedEventArgs e)
    {
        try
        {
            if (!int.TryParse(DownloadLimit.Text, out var limit) || limit < 1) throw new InvalidOperationException("下载上限必须是正整数。");
            var provider = Object("provider");
            provider["base_url"] = ProviderUrl.Text.TrimEnd('/');
            provider["credential_target"] = CredentialTarget.Text.Trim();
            provider["store"] = true;
            provider["enabled"] = true;
            var models = Object(provider, "models");
            SaveModel(models, "research", ResearchModel.Text, ResearchEffort.Text);
            SaveModel(models, "judgment", JudgmentModel.Text, JudgmentEffort.Text);
            SaveModel(models, "fast", FastModel.Text, FastEffort.Text);
            var research = Object("research");
            Object(research, "searxng")["base_url"] = SearxUrl.Text.TrimEnd('/');
            var browser = Object(research, "playwright");
            browser["edge_profile"] = EdgeProfile.Text.Trim(); browser["profile_directory"] = BrowserProfileDirectory.Text.Trim(); browser["download_limit_mb"] = limit;
            if (!string.IsNullOrWhiteSpace(ApiKey.Password)) WindowsCredentialStore.Write(CredentialTarget.Text.Trim(), ApiKey.Password);
            Directory.CreateDirectory(Path.GetDirectoryName(_paths.RuntimeSettingsPath)!);
            var temporary = _paths.RuntimeSettingsPath + ".tmp";
            File.WriteAllText(temporary, _root.ToJsonString(new JsonSerializerOptions { WriteIndented = true }));
            File.Move(temporary, _paths.RuntimeSettingsPath, overwrite: true);
            await _exchange.SendAsync(new { contract = "companion-user-command/v1", command_id = Guid.NewGuid().ToString(), type = "provider.probe" });
            DialogResult = true;
        }
        catch (Exception exception) { MessageBox.Show(this, exception.Message, "配置未保存", MessageBoxButton.OK, MessageBoxImage.Warning); }
    }

    private JsonObject Object(string key) => Object(_root, key);
    private static JsonObject Object(JsonObject parent, string key) => parent[key] as JsonObject ?? (parent[key] = new JsonObject()).AsObject();
    private static string String(JsonObject parent, string key, string fallback) => parent[key]?.GetValue<string>() ?? fallback;
    private static void SetModel(JsonObject models, string key, TextBox id, TextBox effort, string fallback) { var model = Object(models, key); id.Text = String(model, "id", fallback); effort.Text = String(model, "effort", "medium"); }
    private static void SaveModel(JsonObject models, string key, string id, string effort) { if (string.IsNullOrWhiteSpace(id) || string.IsNullOrWhiteSpace(effort)) throw new InvalidOperationException("三个模型槽和 effort 都必须填写。"); var model = Object(models, key); model["id"] = id.Trim(); model["effort"] = effort.Trim(); }
}
