using System.Globalization;
using AIDecisionCenter.Core.Parsing;

namespace AIDecisionCenter.App.Services;

public sealed class LocalInboxService : IDisposable
{
    private readonly AppPaths _paths;
    private readonly ITaskMessageStore _store;
    private readonly AppSettings _settings;
    private readonly SemaphoreSlim _scanLock = new(1, 1);
    private readonly SemaphoreSlim _signal = new(0, 1);
    private readonly HashSet<string> _reconciledProcessedFiles = new(StringComparer.OrdinalIgnoreCase);
    private FileSystemWatcher? _watcher;
    private bool _disposed;

    public LocalInboxService(AppPaths paths, ITaskMessageStore store, AppSettings settings)
    {
        _paths = paths;
        _store = store;
        _settings = settings;
    }

    public async Task RunAsync(
        Func<InboxImportBatch, Task> onImported,
        CancellationToken cancellationToken = default)
    {
        _paths.EnsureDirectories();
        _watcher = new FileSystemWatcher(_paths.PendingDirectory, "*.json")
        {
            IncludeSubdirectories = false,
            NotifyFilter = NotifyFilters.FileName,
            EnableRaisingEvents = true
        };
        _watcher.Created += OnInboxChanged;
        _watcher.Renamed += OnInboxChanged;

        var initial = await ImportAvailableAsync(cancellationToken).ConfigureAwait(false);
        if (initial.Added.Count > 0 || initial.DeadLetterCount > 0 || initial.ReconciliationErrorCount > 0)
        {
            await onImported(initial).ConfigureAwait(false);
        }

        while (!cancellationToken.IsCancellationRequested)
        {
            var interval = TimeSpan.FromSeconds(Math.Clamp(_settings.Inbox.ScanIntervalSeconds, 5, 3600));
            using var timeout = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
            timeout.CancelAfter(interval);
            try
            {
                await _signal.WaitAsync(timeout.Token).ConfigureAwait(false);
                await Task.Delay(
                    TimeSpan.FromMilliseconds(Math.Clamp(_settings.Inbox.DebounceMilliseconds, 50, 5000)),
                    cancellationToken).ConfigureAwait(false);
            }
            catch (OperationCanceledException) when (!cancellationToken.IsCancellationRequested)
            {
                // Periodic scan is the reliability fallback for dropped watcher events.
            }

            var batch = await ImportAvailableAsync(cancellationToken).ConfigureAwait(false);
            if (batch.Added.Count > 0 || batch.DeadLetterCount > 0 || batch.ReconciliationErrorCount > 0)
            {
                await onImported(batch).ConfigureAwait(false);
            }
        }
    }

    public async Task<InboxImportBatch> ImportAvailableAsync(CancellationToken cancellationToken = default)
    {
        await _scanLock.WaitAsync(cancellationToken).ConfigureAwait(false);
        try
        {
            _paths.EnsureDirectories();
            var added = new List<AIDecisionCenter.Core.Models.TaskMessage>();
            var duplicates = 0;
            var deadLetters = 0;
            var recovered = 0;
            var reconciliationErrors = 0;

            var processingFiles = Directory.EnumerateFiles(_paths.ProcessingDirectory, "*.json")
                .OrderBy(path => path, StringComparer.OrdinalIgnoreCase)
                .ToList();
            foreach (var pending in Directory.EnumerateFiles(_paths.PendingDirectory, "*.json")
                         .OrderBy(path => path, StringComparer.OrdinalIgnoreCase))
            {
                var claimed = Path.Combine(_paths.ProcessingDirectory, Path.GetFileName(pending));
                try
                {
                    File.Move(pending, claimed, overwrite: false);
                    processingFiles.Add(claimed);
                }
                catch (IOException) when (!File.Exists(pending))
                {
                    // Another process claimed it first.
                }
                catch (IOException)
                {
                    claimed = Path.Combine(
                        _paths.ProcessingDirectory,
                        $"{Path.GetFileNameWithoutExtension(pending)}.{Guid.NewGuid():N}.json");
                    File.Move(pending, claimed, overwrite: false);
                    processingFiles.Add(claimed);
                }
            }

            foreach (var file in processingFiles.Distinct(StringComparer.OrdinalIgnoreCase))
            {
                cancellationToken.ThrowIfCancellationRequested();
                try
                {
                    var info = new FileInfo(file);
                    if (info.Length > Math.Clamp(_settings.Inbox.MaxMessageBytes, 1024, 100 * 1024 * 1024))
                    {
                        throw new InvalidDataException($"消息超过大小限制：{info.Length} bytes");
                    }

                    var json = await File.ReadAllTextAsync(file, cancellationToken).ConfigureAwait(false);
                    if (!DecisionMessageParser.TryParse(json, DateTimeOffset.UtcNow, out var incoming, out var error) || incoming is null)
                    {
                        throw new InvalidDataException(error ?? "消息契约无效");
                    }

                    var saved = await _store.AddAsync(incoming, cancellationToken).ConfigureAwait(false);
                    if (saved is null)
                    {
                        duplicates++;
                    }
                    else
                    {
                        added.Add(saved);
                    }
                    var processed = MoveProcessed(file, incoming.CompletedAt);
                    _reconciledProcessedFiles.Add(processed);
                }
                catch (Exception exception) when (exception is not OperationCanceledException)
                {
                    MoveDeadLetter(file, exception.Message);
                    deadLetters++;
                }
            }

            foreach (var file in Directory.EnumerateFiles(_paths.ProcessedDirectory, "*.json", SearchOption.AllDirectories)
                         .OrderBy(path => path, StringComparer.OrdinalIgnoreCase))
            {
                if (!_reconciledProcessedFiles.Add(file))
                {
                    continue;
                }
                try
                {
                    var info = new FileInfo(file);
                    if (info.Length > Math.Clamp(_settings.Inbox.MaxMessageBytes, 1024, 100 * 1024 * 1024))
                    {
                        throw new InvalidDataException($"归档消息超过大小限制：{info.Length} bytes");
                    }
                    var json = await File.ReadAllTextAsync(file, cancellationToken).ConfigureAwait(false);
                    if (!DecisionMessageParser.TryParse(json, DateTimeOffset.UtcNow, out var incoming, out var error) || incoming is null)
                    {
                        throw new InvalidDataException(error ?? "归档消息契约无效");
                    }
                    var saved = await _store.AddAsync(incoming, cancellationToken).ConfigureAwait(false);
                    if (saved is not null)
                    {
                        added.Add(saved);
                        recovered++;
                    }
                }
                catch (Exception exception) when (exception is not OperationCanceledException)
                {
                    _reconciledProcessedFiles.Remove(file);
                    reconciliationErrors++;
                }
            }

            return new InboxImportBatch(added, duplicates, deadLetters, recovered, reconciliationErrors);
        }
        finally
        {
            _scanLock.Release();
        }
    }

    public int GetDeadLetterCount()
    {
        _paths.EnsureDirectories();
        return Directory.EnumerateFiles(_paths.DeadLetterDirectory, "*.json").Count();
    }

    public void Dispose()
    {
        if (_disposed)
        {
            return;
        }
        _disposed = true;
        if (_watcher is not null)
        {
            _watcher.Created -= OnInboxChanged;
            _watcher.Renamed -= OnInboxChanged;
            _watcher.Dispose();
        }
        _scanLock.Dispose();
        _signal.Dispose();
    }

    private void OnInboxChanged(object sender, FileSystemEventArgs e)
    {
        try
        {
            _signal.Release();
        }
        catch (SemaphoreFullException)
        {
            // A scan is already queued; one signal is sufficient.
        }
    }

    private string MoveProcessed(string file, DateTimeOffset completedAt)
    {
        var dateDirectory = Path.Combine(
            _paths.ProcessedDirectory,
            completedAt.ToString("yyyy-MM-dd", CultureInfo.InvariantCulture));
        Directory.CreateDirectory(dateDirectory);
        return MoveWithoutOverwrite(file, dateDirectory);
    }

    private void MoveDeadLetter(string file, string error)
    {
        Directory.CreateDirectory(_paths.DeadLetterDirectory);
        var target = MoveWithoutOverwrite(file, _paths.DeadLetterDirectory);
        File.WriteAllText(target + ".error.txt", error, System.Text.Encoding.UTF8);
    }

    private static string MoveWithoutOverwrite(string source, string directory)
    {
        var target = Path.Combine(directory, Path.GetFileName(source));
        if (File.Exists(target))
        {
            target = Path.Combine(
                directory,
                $"{Path.GetFileNameWithoutExtension(source)}.{Guid.NewGuid():N}{Path.GetExtension(source)}");
        }
        File.Move(source, target, overwrite: false);
        return target;
    }
}
