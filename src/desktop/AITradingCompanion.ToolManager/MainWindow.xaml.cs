using System.Windows;
using System.Text.Json;
using System.IO;

namespace AITradingCompanion.ToolManager;

public partial class MainWindow : Window
{
    private readonly ToolManagerProjectionReader _reader;
    public MainWindow(ToolManagerProjectionReader reader) { InitializeComponent(); _reader = reader; Refresh(); }
    private void Refresh_Click(object sender, RoutedEventArgs e) => Refresh();
    private void Pause_Click(object sender, RoutedEventArgs e) => SendNeed("pause");
    private void Retry_Click(object sender, RoutedEventArgs e) => SendNeed("retry");
    private void Disable_Click(object sender, RoutedEventArgs e) => SendTool("disable");
    private void Rollback_Click(object sender, RoutedEventArgs e) => SendTool("rollback");
    private void Refresh() { var projection = _reader.Read(); StatusText.Text = $"{projection.Status}  {projection.UpdatedAt}"; NeedsGrid.ItemsSource = projection.Needs; ToolsGrid.ItemsSource = projection.Tools; }
    private void SendNeed(string type) { if (NeedsGrid.SelectedItem is ToolManagerNeed need) Send(new { type, need_id = need.NeedId }); }
    private void SendTool(string type) { if (ToolsGrid.SelectedItem is ToolManagerTool tool) Send(new { type, capability = tool.Capability }); }
    private static void Send(object command)
    {
        var root = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "AITradingCompanion", "exchange", "to-runtime", "pending");
        Directory.CreateDirectory(root);
        var payload = JsonSerializer.SerializeToUtf8Bytes(new { contract = "ai-trading-tool-manager-command/v1", command_id = Guid.NewGuid().ToString(), command });
        using var document = JsonDocument.Parse(payload);
        var value = document.RootElement.GetProperty("command");
        var envelope = JsonSerializer.SerializeToUtf8Bytes(new { contract = "ai-trading-tool-manager-command/v1", command_id = Guid.NewGuid().ToString(), type = value.GetProperty("type").GetString(), need_id = value.TryGetProperty("need_id", out var need) ? need.GetString() : null, capability = value.TryGetProperty("capability", out var capability) ? capability.GetString() : null });
        File.WriteAllBytes(Path.Combine(root, Guid.NewGuid() + ".json"), envelope);
    }
}
