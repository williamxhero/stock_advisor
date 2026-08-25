using System.Collections.ObjectModel;
using System.Globalization;

namespace AITradingCompanion.Desktop.ViewModels;

public sealed class HistoryDateGroupViewModel
{
    public HistoryDateGroupViewModel(DateOnly date, IEnumerable<HistoryRecordViewModel> records)
    {
        DateText = date.ToDateTime(TimeOnly.MinValue).ToString("yyyy-MM-dd", CultureInfo.InvariantCulture);
        Records = new ObservableCollection<HistoryRecordViewModel>(records);
    }

    public string DateText { get; }
    public ObservableCollection<HistoryRecordViewModel> Records { get; }
}
