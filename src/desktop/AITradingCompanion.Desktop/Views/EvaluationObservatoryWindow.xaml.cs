using System.Windows;
using AITradingCompanion.Desktop.Services;
using AITradingCompanion.Desktop.ViewModels;

namespace AITradingCompanion.Desktop.Views;

public partial class EvaluationObservatoryWindow : Window
{
    private readonly CompanionExchangeService _exchange;
    private readonly EvaluationObservatoryViewModel _viewModel = new();

    public EvaluationObservatoryWindow(CompanionExchangeService exchange)
    {
        InitializeComponent();
        _exchange = exchange;
        DataContext = _viewModel;
        Activated += (_, _) => Refresh();
        Refresh();
    }

    private void RefreshButton_Click(object sender, RoutedEventArgs e) => Refresh();

    private void Refresh() => _viewModel.Refresh(_exchange.ReadLatestEvents(2000));
}
