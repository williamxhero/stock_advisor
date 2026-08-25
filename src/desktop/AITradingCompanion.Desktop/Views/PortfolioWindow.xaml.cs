using System.Windows;
using System.Windows.Controls;
using AITradingCompanion.Desktop.Services;

namespace AITradingCompanion.Desktop.Views;

public partial class PortfolioWindow : Window
{
    private readonly Func<Task> _undoLatest;
    private PortfolioWorkspaceProjection? _projection;

    public PortfolioWindow(Func<Task> undoLatest)
    {
        InitializeComponent();
        _undoLatest = undoLatest;
    }

    public void UpdateProjection(PortfolioWorkspaceProjection? projection)
    {
        _projection = projection;
        PositionsList.ItemsSource = projection?.Positions ?? [];
        TransactionsList.ItemsSource = projection?.Transactions ?? [];
        UndoButton.IsEnabled = projection?.Transactions.Any(item => item.ReversalOf is null && item.Action is "buy" or "sell") == true;
        var totalValue = projection?.Positions.Sum(item => item.MarketValue ?? 0) ?? 0;
        SummaryText.Text = projection is null ? "正在读取本地持仓…" : $"{projection.Positions.Count} 只 · 参考市值 {totalValue:N2} 元 · 更新于 {projection.UpdatedAt ?? "未知"}";
    }

    private async void UndoButton_Click(object sender, RoutedEventArgs e)
    {
        UndoButton.IsEnabled = false;
        try { await _undoLatest(); }
        finally { UndoButton.IsEnabled = _projection?.Transactions.Any(item => item.ReversalOf is null && item.Action is "buy" or "sell") == true; }
    }
}
