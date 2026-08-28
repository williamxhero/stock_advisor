using System.Collections.ObjectModel;
using System.Globalization;
using System.Text.Json.Nodes;
using System.Windows;
using System.Windows.Controls;
using AITradingCompanion.Desktop.Services;
using MessageBox = System.Windows.MessageBox;

namespace AITradingCompanion.Desktop.Views;

/// <summary>Provider maintenance is mediated by the runtime gateway; this window never reads settings.local.json.</summary>
public partial class ProviderSettingsWindow : Window
{
    private static readonly Dictionary<string, string[]> SlotStages = new()
    {
        ["research"] = ["research", "m0_research", "m1_research", "outcome_research", "chat_research"],
        ["judgment"] = ["judgment", "m1_judgment", "m2", "reflection", "workflow_feedback"],
        ["fast"] = ["fast", "m0_compose", "chat"],
    };
    private readonly ICompanionGateway _gateway;
    private JsonObject _config = new();
    private JsonObject _current = new();
    private bool _loading;
    private bool _isNew;
    private bool _requiresNewKey;
    private readonly Dictionary<string, string> _quality = new(StringComparer.Ordinal);

    public ObservableCollection<RouteEditor> Routes { get; } = [];
    public ObservableCollection<string> AvailableModels { get; } = [];
    public ObservableCollection<ModelCatalogEditor> ModelCatalog { get; } = [];

    public sealed class ProviderListItem
    {
        public required string Id { get; init; }
        public required JsonObject Endpoint { get; init; }
        public required string Display { get; init; }
    }

    public sealed class RouteEditor
    {
        public required string Family { get; init; }
        public required string Slot { get; init; }
        public string Model { get; set; } = string.Empty;
        public int Tier { get; set; }
        public string TierMode { get; set; } = "auto";
        public int Preference { get; set; }
        public string Effort { get; set; } = "medium";
        public bool Enabled { get; set; } = true;
        public JsonArray Capabilities { get; init; } = [];
    }

    public sealed class ModelCatalogEditor
    {
        public required string Family { get; init; }
        public required string Model { get; init; }
        public string Aliases { get; set; } = string.Empty;
        public double Input { get; set; }
        public double CachedInput { get; set; }
        public double Output { get; set; }
        public string Quality { get; set; } = "0/0/0";
    }

    public ProviderSettingsWindow(ICompanionGateway gateway)
    {
        InitializeComponent();
        DataContext = this;
        _gateway = gateway;
        StatusFilter.SelectedIndex = 0;
        SortFilter.SelectedIndex = 0;
    }

    private async void Window_Loaded(object sender, RoutedEventArgs e) => await RefreshAsync();

    private async Task RefreshAsync(string? select = null)
    {
        try
        {
            _loading = true;
            _config = await _gateway.GetSnapshotAsync("provider-config");
            var summary = await _gateway.GetProviderQualityAsync();
            _quality.Clear();
            if (summary["items"] is JsonArray qualityItems)
            {
                foreach (var item in qualityItems.OfType<JsonObject>())
                {
                    var endpoint = Text(item, "endpoint", "");
                    if (endpoint.Length == 0) continue;
                    var rate = Number(item, "product_success_rate");
                    var samples = Int(item, "sample_size");
                    _quality[endpoint] = samples == 0 ? "数据不足" : $"产品 {rate:P0} · n={samples}";
                }
            }
            RebuildList(select);
            LoadModelCatalog();
        }
        catch (Exception exception)
        {
            MessageBox.Show(this, $"无法读取本地 Provider 配置：{exception.Message}", "Provider 管理", MessageBoxButton.OK, MessageBoxImage.Warning);
        }
        finally { _loading = false; }
    }

    private void FilterChanged(object sender, EventArgs e)
    {
        if (!_loading) RebuildList((_current["id"]?.GetValue<string>()));
    }

    private void RebuildList(string? select)
    {
        var endpoints = Provider()["endpoints"] as JsonArray ?? [];
        var query = endpoints.OfType<JsonObject>().Where(Matches).Select(endpoint => new ProviderListItem
        {
            Id = Text(endpoint, "id", ""), Endpoint = endpoint, Display = Display(endpoint),
        });
        var ordered = ((SortFilter.SelectedItem as ComboBoxItem)?.Content?.ToString()) switch
        {
            "倍率" => query.OrderBy(item => Number(item.Endpoint, "weight", 0.3)).ThenBy(item => item.Id, StringComparer.Ordinal),
            "产品成功率" => query.OrderByDescending(item => QualityScore(item.Id)).ThenBy(item => item.Id, StringComparer.Ordinal),
            _ => query.OrderByDescending(item => Text(item.Endpoint, "updated_at", "")).ThenBy(item => item.Id, StringComparer.Ordinal),
        };
        var items = ordered.ToList();
        ProviderList.ItemsSource = items;
        var selected = items.FirstOrDefault(item => item.Id == select) ?? items.FirstOrDefault();
        ProviderList.SelectedItem = selected;
        if (selected is not null) LoadEndpoint(selected.Endpoint);
    }

    private bool Matches(JsonObject endpoint)
    {
        var status = (StatusFilter.SelectedItem as ComboBoxItem)?.Content?.ToString();
        if (status == "已启用" && (!Bool(endpoint, "enabled") || Bool(endpoint, "archived"))) return false;
        if (status == "已停用" && (Bool(endpoint, "enabled") || Bool(endpoint, "archived"))) return false;
        if (status == "已归档" && !Bool(endpoint, "archived")) return false;
        var term = SearchBox.Text.Trim();
        return term.Length == 0 || Text(endpoint, "id", "").Contains(term, StringComparison.OrdinalIgnoreCase)
            || Host(endpoint).Contains(term, StringComparison.OrdinalIgnoreCase);
    }

    private string Display(JsonObject endpoint)
    {
        var state = Bool(endpoint, "archived") ? "已归档" : Bool(endpoint, "enabled") ? "启用" : "停用";
        var id = Text(endpoint, "id", "未命名");
        var quality = _quality.GetValueOrDefault(id, "数据不足");
        return $"{id}\n{Host(endpoint)} · {state} · {Number(endpoint, "weight", 0.3):0.###}×\n{quality}";
    }

    private void ProviderList_SelectionChanged(object sender, SelectionChangedEventArgs e)
    {
        if (_loading || ProviderList.SelectedItem is not ProviderListItem item) return;
        _isNew = false; _requiresNewKey = false; LoadEndpoint(item.Endpoint);
    }

    private void LoadEndpoint(JsonObject endpoint)
    {
        _loading = true;
        _current = endpoint;
        ProviderId.Text = Text(endpoint, "id", "");
        ProviderId.IsReadOnly = !_isNew;
        ProviderUrl.Text = Text(endpoint, "base_url", "");
        WeightBox.Text = Number(endpoint, "weight", 0.3).ToString("0.###", CultureInfo.InvariantCulture);
        ApiKey.Clear();
        var hint = Text(endpoint, "api_key_hint", "");
        ApiKeyHint.Text = hint.Length == 0 ? "未配置" : $"当前：{hint}（留空保持原值）";
        AvailableModels.Clear();
        foreach (var model in (endpoint["available_models"] as JsonArray ?? []).Select(node => node?.GetValue<string>() ?? "").Where(model => model.Length > 0)) AvailableModels.Add(model);
        var modelsUpdatedAt = Text(endpoint, "models_updated_at", "");
        ModelsText.Text = AvailableModels.Count == 0
            ? "尚未读取模型列表；可手动填写模型。"
            : $"已读取 {AvailableModels.Count} 个模型{(modelsUpdatedAt.Length == 0 ? "" : $"（{modelsUpdatedAt}）")}。";
        EnabledBox.IsChecked = Bool(endpoint, "enabled") && !Bool(endpoint, "archived");
        var families = Families(endpoint);
        OpenAiFamily.IsChecked = families.Contains("openai");
        AnthropicFamily.IsChecked = families.Contains("anthropic");
        SelectFamilyMode(Text(Routing(), "family_mode", "auto"));
        StatusText.Text = Bool(endpoint, "archived") ? "已归档；恢复后才可使用。" : _quality.GetValueOrDefault(Text(endpoint, "id", ""), "尚无质量样本");
        Routes.Clear();
        foreach (var family in families)
        foreach (var slot in SlotStages.Keys)
        {
            var route = ExistingRoute(Text(endpoint, "id", ""), family, slot);
            Routes.Add(new RouteEditor
            {
                Family = family, Slot = slot,
                Model = route is null ? string.Empty : Text(route, "model", ""),
                Tier = route is null ? 0 : CostTier(route),
                TierMode = route is null ? "auto" : Text(route, "tier_mode", "manual"),
                Preference = route is null ? 0 : Int(route, "preference", 0),
                Effort = route is null ? "medium" : Text(route, "effort", "medium"),
                Enabled = route is null || Bool(route, "enabled", true),
                Capabilities = route?["capabilities"]?.DeepClone() as JsonArray ?? new JsonArray("json_schema"),
            });
        }
        _loading = false;
    }

    private void FamilyChanged(object sender, RoutedEventArgs e)
    {
        if (_loading) return;
        var families = SelectedFamilies();
        var preserved = Routes.ToDictionary(route => $"{route.Family}/{route.Slot}");
        Routes.Clear();
        foreach (var family in families)
        foreach (var slot in SlotStages.Keys)
        {
            if (preserved.TryGetValue($"{family}/{slot}", out var existing)) Routes.Add(existing);
            else Routes.Add(new RouteEditor { Family = family, Slot = slot, Capabilities = new JsonArray("json_schema") });
        }
    }

    private void New_Click(object sender, RoutedEventArgs e)
    {
        _isNew = true; _requiresNewKey = true;
        LoadEndpoint(new JsonObject { ["weight"] = 0.3, ["enabled"] = true, ["families"] = new JsonArray("openai") });
        ProviderId.Clear(); ProviderId.IsReadOnly = false; StatusText.Text = "新 Provider：必须填写唯一 ID、URL、密钥和所有模型槽。";
    }

    private void Copy_Click(object sender, RoutedEventArgs e)
    {
        if (_current.Count == 0) return;
        _isNew = true; _requiresNewKey = true; LoadEndpoint(_current.DeepClone().AsObject());
        ProviderId.Text = $"{ProviderId.Text}-copy"; ProviderId.IsReadOnly = false;
        ApiKey.Clear(); StatusText.Text = "复制只带非秘密配置；请为新 Provider 填写 API key。";
    }

    private async void Probe_Click(object sender, RoutedEventArgs e) => await ExecuteActionAsync("probe", false);
    private async void RefreshModels_Click(object sender, RoutedEventArgs e) => await ExecuteActionAsync("refresh_models", false);
    private async void Archive_Click(object sender, RoutedEventArgs e) => await ExecuteActionAsync("archive", false);
    private async void Restore_Click(object sender, RoutedEventArgs e) => await ExecuteActionAsync("restore", false);

    private async void Delete_Click(object sender, RoutedEventArgs e)
    {
        if (MessageBox.Show(this, "永久删除只移除当前配置及路由，统计审计会保留。继续吗？", "永久删除", MessageBoxButton.YesNo, MessageBoxImage.Warning) != MessageBoxResult.Yes) return;
        await ExecuteActionAsync("permanent_delete", true);
    }

    private async Task ExecuteActionAsync(string action, bool confirmed)
    {
        var id = Text(_current, "id", "");
        if (id.Length == 0) return;
        try
        {
            await _gateway.SubmitCommandAsync(new JsonObject
            {
                ["contract"] = "provider-management/v1", ["command_id"] = Guid.NewGuid().ToString(),
                ["action"] = action, ["id"] = id, ["confirmed"] = confirmed,
            });
            await RefreshAsync(action == "permanent_delete" ? null : id);
        }
        catch (Exception exception) { MessageBox.Show(this, exception.Message, "Provider 管理", MessageBoxButton.OK, MessageBoxImage.Warning); }
    }

    private async void Save_Click(object sender, RoutedEventArgs e)
    {
        try
        {
            RoutesGrid.CommitEdit(DataGridEditingUnit.Cell, true); RoutesGrid.CommitEdit(DataGridEditingUnit.Row, true);
            var endpoint = BuildEndpoint();
            var routes = BuildRoutes(Text(endpoint, "id", ""));
            var changed = _isNew ? "创建 Provider 与其模型槽" : "更新 Provider、端点倍率和模型槽";
            if (MessageBox.Show(this, $"将{changed}。端点倍率会同步到全部 relative 路由；保存后运行时会热加载并轻量探测。继续吗？", "确认变更", MessageBoxButton.YesNo, MessageBoxImage.Question) != MessageBoxResult.Yes) return;
            var payload = new JsonObject
            {
                ["contract"] = "provider-management/v1", ["command_id"] = Guid.NewGuid().ToString(), ["action"] = "upsert",
                ["endpoint"] = endpoint, ["routes"] = routes,
                ["family_mode"] = ((FamilyMode.SelectedItem as ComboBoxItem)?.Tag?.ToString() ?? "auto"),
            };
            if (!_isNew) payload["original_id"] = Text(_current, "id", "");
            if (!string.IsNullOrWhiteSpace(ApiKey.Password)) payload["api_key"] = ApiKey.Password;
            await _gateway.SubmitCommandAsync(payload);
            await RefreshAsync(Text(endpoint, "id", ""));
            _isNew = false; _requiresNewKey = false;
        }
        catch (Exception exception) { MessageBox.Show(this, exception.Message, "配置未保存", MessageBoxButton.OK, MessageBoxImage.Warning); }
    }

    private void LoadModelCatalog()
    {
        ModelCatalog.Clear();
        if (Routing()["model_catalog"] is not JsonObject families) return;
        foreach (var (family, modelsNode) in families)
        {
            if (modelsNode is not JsonObject models) continue;
            foreach (var (model, itemNode) in models)
            {
                if (itemNode is not JsonObject item) continue;
                var price = item["price"] as JsonObject ?? item;
                var quality = item["quality"] as JsonObject ?? new JsonObject();
                var aliases = (item["aliases"] as JsonArray ?? []).Select(node => node?.GetValue<string>() ?? "").Where(value => value.Length > 0);
                ModelCatalog.Add(new ModelCatalogEditor
                {
                    Family = family, Model = model, Aliases = string.Join(", ", aliases),
                    Input = Number(price, "input_per_million"), CachedInput = Number(price, "cached_input_per_million", Number(price, "input_per_million")), Output = Number(price, "output_per_million"),
                    Quality = $"{Int(quality, "research")}/{Int(quality, "judgment")}/{Int(quality, "fast")}",
                });
            }
        }
    }

    private async void SaveCatalog_Click(object sender, RoutedEventArgs e)
    {
        try
        {
            CatalogGrid.CommitEdit(DataGridEditingUnit.Cell, true); CatalogGrid.CommitEdit(DataGridEditingUnit.Row, true);
            var catalog = new JsonObject();
            foreach (var row in ModelCatalog)
            {
                if (row.Input < 0 || row.CachedInput < 0 || row.Output < 0) throw new InvalidOperationException("模型价格不能为负数。");
                var scores = row.Quality.Split('/');
                if (scores.Length != 3 || !int.TryParse(scores[0], out var research) || !int.TryParse(scores[1], out var judgment) || !int.TryParse(scores[2], out var fast)
                    || new[] { research, judgment, fast }.Any(score => score is < 0 or > 100)) throw new InvalidOperationException("能力分请按 research/judgment/fast 以 0–100 整数填写，例如 92/90/88。");
                if (catalog[row.Family] is not JsonObject models) { models = new JsonObject(); catalog[row.Family] = models; }
                models[row.Model] = new JsonObject
                {
                    ["aliases"] = new JsonArray(row.Aliases.Split(',', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries).Select(alias => JsonValue.Create(alias)).ToArray()),
                    ["price"] = new JsonObject { ["currency"] = "USD", ["input_per_million"] = row.Input, ["cached_input_per_million"] = row.CachedInput, ["output_per_million"] = row.Output },
                    ["quality"] = new JsonObject { ["research"] = research, ["judgment"] = judgment, ["fast"] = fast },
                };
            }
            if (MessageBox.Show(this, "保存模型目录会重算所有自动 Tier，手动锁定的 Tier 不变。继续吗？", "确认模型目录", MessageBoxButton.YesNo, MessageBoxImage.Question) != MessageBoxResult.Yes) return;
            await _gateway.SubmitCommandAsync(new JsonObject { ["contract"] = "provider-management/v1", ["command_id"] = Guid.NewGuid().ToString(), ["action"] = "update_model_catalog", ["model_catalog"] = catalog });
            await RefreshAsync(Text(_current, "id", ""));
        }
        catch (Exception exception) { MessageBox.Show(this, exception.Message, "模型目录未保存", MessageBoxButton.OK, MessageBoxImage.Warning); }
    }

    private JsonObject BuildEndpoint()
    {
        var id = ProviderId.Text.Trim();
        if (id.Length == 0 || id.Any(character => !(char.IsLetterOrDigit(character) || character is '-' or '_'))) throw new InvalidOperationException("Provider ID 必须唯一且只含字母、数字、- 或 _。");
        var url = ProviderUrl.Text.Trim().TrimEnd('/');
        if (!Uri.TryCreate(url, UriKind.Absolute, out var uri) || !(uri.Scheme is "http" or "https") || string.IsNullOrWhiteSpace(uri.Host)) throw new InvalidOperationException("请填写有效的 http(s) API 根地址或完整端点 URL。");
        if (!double.TryParse(WeightBox.Text, NumberStyles.Float, CultureInfo.InvariantCulture, out var weight) || weight <= 0) throw new InvalidOperationException("倍率必须是大于 0 的数字。");
        var families = SelectedFamilies();
        if (families.Count == 0) throw new InvalidOperationException("至少选择一个模型家族。");
        if ((_isNew || _requiresNewKey) && string.IsNullOrWhiteSpace(ApiKey.Password)) throw new InvalidOperationException("新建或复制 Provider 必须填写 API key。");
        var endpoint = new JsonObject { ["id"] = id, ["base_url"] = url, ["weight"] = weight, ["enabled"] = EnabledBox.IsChecked == true, ["archived"] = false, ["families"] = new JsonArray(families.Select(family => JsonValue.Create(family)).ToArray()) };
        PreserveInventoryMetadata(_current, endpoint);
        return endpoint;
    }

    private static void PreserveInventoryMetadata(JsonObject source, JsonObject target)
    {
        target["available_models"] = source["available_models"]?.DeepClone();
        target["model_directory_status"] = source["model_directory_status"]?.DeepClone();
        target["models_updated_at"] = source["models_updated_at"]?.DeepClone();
    }

    private JsonArray BuildRoutes(string endpointId)
    {
        var expected = SelectedFamilies().Count * SlotStages.Count;
        if (Routes.Count != expected) throw new InvalidOperationException("请完整填写每个已选家族的 research、judgment、fast 模型槽。");
        var routes = new JsonArray();
        foreach (var route in Routes)
        {
            if (string.IsNullOrWhiteSpace(route.Model) || string.IsNullOrWhiteSpace(route.Effort) || route.Tier < 0) throw new InvalidOperationException("模型、effort 和非负 tier 都必须填写。");
            routes.Add(new JsonObject
            {
                ["id"] = $"{endpointId}-{route.Family}-{route.Slot}", ["endpoint"] = endpointId, ["model"] = route.Model.Trim(), ["model_family"] = route.Family, ["transport"] = route.Family == "openai" ? "responses" : "chat_completions",
                ["enabled"] = route.Enabled, ["stages"] = new JsonArray(SlotStages[route.Slot].Select(stage => JsonValue.Create(stage)).ToArray()), ["slot"] = route.Slot,
                ["capabilities"] = route.Capabilities.DeepClone(), ["effort"] = route.Effort.Trim(), ["tier_mode"] = route.TierMode, ["tier"] = route.Tier, ["preference"] = route.Preference,
                ["cost"] = new JsonObject { ["tier"] = route.Tier, ["mode"] = "relative" },
            });
        }
        return routes;
    }

    private JsonObject Provider() => _config["provider"] as JsonObject ?? new JsonObject();
    private JsonObject Routing() => Provider()["routing"] as JsonObject ?? new JsonObject();
    private JsonObject? ExistingRoute(string endpoint, string family, string slot) =>
        (Provider()["routes"] as JsonArray ?? []).OfType<JsonObject>().FirstOrDefault(route => Text(route, "endpoint", "") == endpoint && Text(route, "model_family", "") == family && Slot(route) == slot);
    private static string Slot(JsonObject route) => Text(route, "slot", "") is { Length: > 0 } slot ? slot : SlotStages.First(pair => (route["stages"] as JsonArray ?? []).Select(node => node?.GetValue<string>() ?? "").Any(pair.Value.Contains)).Key;
    private static List<string> Families(JsonObject endpoint)
    {
        var values = (endpoint["families"] as JsonArray ?? []).Select(node => node?.GetValue<string>() ?? "").Where(value => value is "openai" or "anthropic").Distinct().ToList();
        return values;
    }
    private List<string> SelectedFamilies()
    {
        var values = new List<string>(); if (OpenAiFamily.IsChecked == true) values.Add("openai"); if (AnthropicFamily.IsChecked == true) values.Add("anthropic"); return values;
    }
    private void SelectFamilyMode(string mode) => FamilyMode.SelectedItem = FamilyMode.Items.OfType<ComboBoxItem>().FirstOrDefault(item => item.Tag?.ToString() == mode) ?? FamilyMode.Items[0];
    private static string Host(JsonObject endpoint) => Uri.TryCreate(Text(endpoint, "base_url", ""), UriKind.Absolute, out var uri) ? uri.Host : "URL 未配置";
    private double QualityScore(string endpoint) => _quality.TryGetValue(endpoint, out var value) && value.StartsWith("产品 ", StringComparison.Ordinal) && double.TryParse(value.Split(' ')[1].TrimEnd('%'), NumberStyles.Float, CultureInfo.InvariantCulture, out var rate) ? rate : -1;
    private static string Text(JsonObject node, string key, string fallback) => node[key] is JsonValue value && value.TryGetValue<string>(out var text) ? text ?? fallback : fallback;
    private static int Int(JsonObject node, string key, int fallback = 0) => node[key] is JsonValue value && value.TryGetValue<int>(out var number) ? number : fallback;
    private static int CostTier(JsonObject route) => route["cost"] is JsonObject cost ? Int(cost, "tier", Int(route, "tier", 0)) : Int(route, "tier", 0);
    private static double Number(JsonObject node, string key, double fallback = 0) => node[key] is JsonValue value && value.TryGetValue<double>(out var number) ? number : fallback;
    private static bool Bool(JsonObject node, string key, bool fallback = false) => node[key] is JsonValue value && value.TryGetValue<bool>(out var result) ? result : fallback;
}
