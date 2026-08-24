using System.ComponentModel;
using System.Windows;
using AIDecisionCenter.App.Converters;
using AIDecisionCenter.App.ViewModels;

namespace AIDecisionCenter.App.Views;

public partial class MainWindow : Window
{
    private readonly MainViewModel _viewModel;

    public MainWindow(MainViewModel viewModel)
    {
        InitializeComponent();
        _viewModel = viewModel;
        DataContext = viewModel;
        viewModel.PropertyChanged += OnViewModelPropertyChanged;
        Closed += OnClosed;
        UpdateDocument();
    }

    private void OnViewModelPropertyChanged(object? sender, PropertyChangedEventArgs e)
    {
        if (e.PropertyName == nameof(MainViewModel.SelectedMessage))
        {
            UpdateDocument();
        }
    }

    private void OnClosed(object? sender, EventArgs e)
    {
        _viewModel.PropertyChanged -= OnViewModelPropertyChanged;
        Closed -= OnClosed;
    }

    private void UpdateDocument()
    {
        MarkdownViewer.Document = MarkdownDocumentBuilder.Build(_viewModel.SelectedMessage?.BodyMarkdown);
    }
}
