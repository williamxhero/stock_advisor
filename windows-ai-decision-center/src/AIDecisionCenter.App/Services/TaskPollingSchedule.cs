using AIDecisionCenter.Core.Models;

namespace AIDecisionCenter.App.Services;

internal static class TaskPollingSchedule
{
    public static TimeSpan GetDelay(
        DateTime now,
        DateOnly completedSlotsDate,
        IEnumerable<TimeOnly> completedSlots,
        DateTime? lastCheckAt,
        TimeSpan retryInterval,
        TimeSpan nodeTimeout)
    {
        ArgumentOutOfRangeException.ThrowIfLessThanOrEqual(retryInterval, TimeSpan.Zero);
        ArgumentOutOfRangeException.ThrowIfLessThanOrEqual(nodeTimeout, TimeSpan.Zero);

        var today = DateOnly.FromDateTime(now);
        var completed = completedSlotsDate == today ? completedSlots.ToHashSet() : [];
        var activeIncomplete = ExpectedTaskCatalog.AShareTasks
            .Where(task => !completed.Contains(task.Slot))
            .Select(task => new
            {
                Task = task,
                DueAt = now.Date.Add(task.Slot.ToTimeSpan())
            })
            .Where(candidate => candidate.DueAt <= now && now < candidate.DueAt.Add(nodeTimeout))
            .ToArray();

        if (activeIncomplete.Length > 0)
        {
            var latestDueAt = activeIncomplete[^1].DueAt;
            if (lastCheckAt is null || lastCheckAt.Value < latestDueAt)
            {
                return TimeSpan.Zero;
            }

            var retryAt = lastCheckAt.Value.Add(retryInterval);
            var deadline = latestDueAt.Add(nodeTimeout);
            var nextWakeAt = retryAt < deadline ? retryAt : deadline;
            return nextWakeAt > now ? nextWakeAt - now : TimeSpan.Zero;
        }

        var currentTime = TimeOnly.FromDateTime(now);
        var nextToday = ExpectedTaskCatalog.AShareTasks
            .FirstOrDefault(task => task.Slot > currentTime && !completed.Contains(task.Slot));
        if (nextToday is not null)
        {
            return now.Date.Add(nextToday.Slot.ToTimeSpan()) - now;
        }

        var firstTomorrow = ExpectedTaskCatalog.AShareTasks[0];
        return now.Date.AddDays(1).Add(firstTomorrow.Slot.ToTimeSpan()) - now;
    }
}
