namespace AITradingCompanion.Core.Models;

public sealed record ExpectedTask(string TaskKey, TimeOnly Slot, string Name);
