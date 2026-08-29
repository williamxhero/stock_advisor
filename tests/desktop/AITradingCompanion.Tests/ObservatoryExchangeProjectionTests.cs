using AITradingCompanion.Desktop.Services;

namespace AITradingCompanion.Tests;

public sealed class ObservatoryExchangeProjectionTests
{
    [Fact]
    public void ProjectsVersionOneSnapshotsIntoFourReadOnlyPagesAndIgnoresUnknownVersions()
    {
        var messages = new[]
        {
            """{"contract":"evaluation-observatory-snapshot/v1","contract_version":1,"message_id":"e1","created_at":"2026-08-29T02:30:00Z","snapshot":{"snapshot_id":"e1","snapshot_kind":"evaluation","created_at":"2026-08-29T02:30:00Z","source_fingerprint":"a","task_key":"daily.execution.0945","delivery_state":"qualified","planned_start_at":"2026-08-29T09:45:00+08:00","actual_start_at":"2026-08-29T09:50:00+08:00","qualified_published_at":"2026-08-29T10:20:00+08:00","research_quality":{"evidence_coverage":0.8,"max_freshness_age_seconds":900,"independent_source_groups":2,"conflict_count":1,"factual_error_count":0,"evidence_gate_passed":true,"rejection_reasons":[],"completeness":["gate_fact"]},"judgment_outcomes":[{"horizon":"T+1","original_direction":"bullish","checkpoint_status":"complete","verification_status":"correct"}]}}""",
            """{"contract":"evaluation-observatory-snapshot/v1","contract_version":1,"message_id":"f1","created_at":"2026-08-29T02:00:00Z","snapshot":{"snapshot_id":"f1","snapshot_kind":"forecast","created_at":"2026-08-29T02:00:00Z","source_fingerprint":"b","task_key":"daily.execution.0945","qualified_probability":0.75,"wilson_90_low":0.35,"wilson_90_high":0.94,"maturity":"low"}}""",
            """{"contract":"evaluation-observatory-snapshot/v1","contract_version":1,"message_id":"x1","created_at":"2026-08-29T03:00:00Z","snapshot":{"snapshot_id":"x1","snapshot_kind":"experiment","created_at":"2026-08-29T03:00:00Z","source_fingerprint":"c","experiment_key":"m1:daily.execution.1430","paired_runs":9,"decision":"recommend_promotion","decision_reasons":["material_improvement"]}}""",
            """{"contract":"evaluation-observatory-snapshot/v2","contract_version":2,"message_id":"future","created_at":"2026-08-29T04:00:00Z","snapshot":{"snapshot_id":"future","snapshot_kind":"experiment","created_at":"2026-08-29T04:00:00Z","source_fingerprint":"d"}}""",
        };

        var dashboard = ObservatoryExchangeProjection.Project(messages);

        Assert.Equal(2, dashboard.RuntimeHealth.Count);
        Assert.Single(dashboard.ResearchQuality);
        Assert.Single(dashboard.JudgmentOutcomes);
        Assert.Single(dashboard.EvolutionExperiments);
        var visible = string.Join("\n", dashboard.All.Select(item => $"{item.Title} {item.Summary} {item.Detail}"));
        Assert.DoesNotContain("effort", visible, StringComparison.OrdinalIgnoreCase);
        Assert.DoesNotContain("token", visible, StringComparison.OrdinalIgnoreCase);
        Assert.DoesNotContain("model", visible, StringComparison.OrdinalIgnoreCase);
        Assert.DoesNotContain("future", visible, StringComparison.OrdinalIgnoreCase);
    }

    [Fact]
    public void ObservatoryWindowUsesFourExistingWpfTabsAndKeepsProviderDiagnosticsOut()
    {
        var root = new DirectoryInfo(AppContext.BaseDirectory);
        while (root is not null && !File.Exists(Path.Combine(root.FullName, "AITradingCompanion.sln"))) root = root.Parent;
        Assert.NotNull(root);
        var xaml = File.ReadAllText(Path.Combine(root.FullName!, "src", "desktop", "AITradingCompanion.Desktop", "Views", "EvaluationObservatoryWindow.xaml"));
        var main = File.ReadAllText(Path.Combine(root.FullName!, "src", "desktop", "AITradingCompanion.Desktop", "Views", "MainWindow.xaml"));

        Assert.Contains("Header=\"运行状况\"", xaml, StringComparison.Ordinal);
        Assert.Contains("Header=\"研究质量\"", xaml, StringComparison.Ordinal);
        Assert.Contains("Header=\"判断结果\"", xaml, StringComparison.Ordinal);
        Assert.Contains("Header=\"进化实验\"", xaml, StringComparison.Ordinal);
        Assert.Contains("Content=\"评测与进化中心\"", main, StringComparison.Ordinal);
        Assert.DoesNotContain("effort", xaml, StringComparison.OrdinalIgnoreCase);
        Assert.DoesNotContain("token", xaml, StringComparison.OrdinalIgnoreCase);
        Assert.DoesNotContain("model", xaml, StringComparison.OrdinalIgnoreCase);
    }
}
