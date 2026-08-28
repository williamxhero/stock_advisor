using System.Windows;
using AITradingCompanion.Desktop.Services;
using AITradingCompanion.Desktop.ViewModels;
using MessageBox = System.Windows.MessageBox;
using SaveFileDialog = Microsoft.Win32.SaveFileDialog;

namespace AITradingCompanion.Desktop.Views;

public partial class ProviderQualityWindow : Window, IDisposable
{
    private readonly ProviderQualityViewModel _viewModel;
    private bool _disposed;

    public ProviderQualityWindow(AppPaths paths)
    {
        InitializeComponent();
        _viewModel = new ProviderQualityViewModel(new LoopbackHttpGateway(paths));
        DataContext = _viewModel;
        Loaded += async (_, _) => await _viewModel.RefreshAsync();
        Closed += (_, _) => Dispose();
    }
    private async void Refresh_Click(object sender, RoutedEventArgs e) => await _viewModel.RefreshAsync();
    private async void ExportCsv_Click(object sender, RoutedEventArgs e) => await ExportAsync("csv");
    private async void ExportJson_Click(object sender, RoutedEventArgs e) => await ExportAsync("json");
    private async Task ExportAsync(string format)
    {
        try
        {
            var dialog = new SaveFileDialog { FileName = $"provider-quality-{DateTime.Now:yyyyMMdd-HHmm}.{format}",
                Filter = format == "csv" ? "CSV 文件|*.csv" : "JSON 文件|*.json" };
            if (dialog.ShowDialog(this) != true) return;
            await File.WriteAllTextAsync(dialog.FileName, await _viewModel.ExportAsync(format));
            MessageBox.Show(this, $"脱敏统计已导出到：\n{dialog.FileName}", "导出完成", MessageBoxButton.OK, MessageBoxImage.Information);
        }
        catch (Exception exception) { MessageBox.Show(this, exception.Message, "导出失败", MessageBoxButton.OK, MessageBoxImage.Warning); }
    }

    public void Dispose()
    {
        if (_disposed) return;
        _disposed = true;
        _viewModel.Dispose();
        GC.SuppressFinalize(this);
    }
}
