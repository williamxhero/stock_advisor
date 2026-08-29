using System.Collections.ObjectModel;
using AITradingCompanion.Desktop.Services;

namespace AITradingCompanion.Desktop.ViewModels;

public sealed class EvaluationObservatoryViewModel
{
    public ObservableCollection<ObservatoryCard> RuntimeHealth { get; } = [];
    public ObservableCollection<ObservatoryCard> ResearchQuality { get; } = [];
    public ObservableCollection<ObservatoryCard> JudgmentOutcomes { get; } = [];
    public ObservableCollection<ObservatoryCard> EvolutionExperiments { get; } = [];

    public void Refresh(IEnumerable<string> exchangeMessages)
    {
        var dashboard = ObservatoryExchangeProjection.Project(exchangeMessages);
        Replace(RuntimeHealth, dashboard.RuntimeHealth);
        Replace(ResearchQuality, dashboard.ResearchQuality);
        Replace(JudgmentOutcomes, dashboard.JudgmentOutcomes);
        Replace(EvolutionExperiments, dashboard.EvolutionExperiments);
    }

    private static void Replace(ObservableCollection<ObservatoryCard> target, IEnumerable<ObservatoryCard> values)
    {
        target.Clear();
        foreach (var value in values) target.Add(value);
    }
}
