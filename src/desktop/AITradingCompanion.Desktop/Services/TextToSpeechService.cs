using System.Diagnostics;
using System.Security.Cryptography;
using System.Text;
using System.Windows.Media;

namespace AITradingCompanion.Desktop.Services;

public sealed class TextToSpeechService : IDisposable
{
    private const string SettingsVersion = "hsiaoyu-rate-6-pitch-22-volume-4db-v1";
    private const int MaxCachedFiles = 50;

    private readonly object _gate = new();
    private readonly string _cacheDirectory;
    private readonly string _scriptPath;
    private readonly string _pythonPath;
    private readonly string _voiceName = "Edge HsiaoYu Neural · 1.3×";
    private Process? _generator;
    private MediaPlayer? _player;
    private int _operationId;
    private bool _disposed;

    public TextToSpeechService(AppPaths paths)
    {
        _cacheDirectory = Path.Combine(paths.DataDirectory, "tts-cache");
        _scriptPath = Path.Combine(AppContext.BaseDirectory, "Tts", "edge_tts_reader.py");
        _pythonPath = ResolveExecutable(
            Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                "Programs", "Python", "Python313", "python.exe"),
            "python.exe");
    }

    public event EventHandler? StateChanged;

    public bool IsSpeaking { get; private set; }

    public SpeechState State { get; private set; }

    public string VoiceName => _voiceName;

    public async Task SpeakAsync(string text)
    {
        ObjectDisposedException.ThrowIf(_disposed, this);
        if (string.IsNullOrWhiteSpace(text))
        {
            return;
        }

        Stop();
        var operationId = Interlocked.Increment(ref _operationId);
        SetState(SpeechState.Generating);
        Directory.CreateDirectory(_cacheDirectory);
        var audioPath = Path.Combine(_cacheDirectory, $"message-{ComputeCacheKey(text)}.mp3");

        try
        {
            if (!File.Exists(audioPath) || new FileInfo(audioPath).Length == 0)
            {
                await GenerateAsync(text, audioPath, operationId);
            }

            if (operationId != Volatile.Read(ref _operationId))
            {
                return;
            }
            StartPlayback(audioPath, operationId);
            TrimCache();
        }
        catch
        {
            if (operationId == Volatile.Read(ref _operationId))
            {
                SetState(SpeechState.Idle);
            }
            throw;
        }
    }

    public void Stop()
    {
        if (_disposed)
        {
            return;
        }

        Interlocked.Increment(ref _operationId);
        Process? generator;
        MediaPlayer? player;
        lock (_gate)
        {
            generator = _generator;
            player = _player;
            _generator = null;
            _player = null;
        }

        StopProcess(generator, ownsProcess: false);
        ClosePlayer(player);
        SetState(SpeechState.Idle);
    }

    public void Dispose()
    {
        if (_disposed)
        {
            return;
        }
        Stop();
        _disposed = true;
        GC.SuppressFinalize(this);
    }

    private async Task GenerateAsync(string text, string audioPath, int operationId)
    {
        if (!File.Exists(_scriptPath))
        {
            throw new FileNotFoundException("高级TTS脚本不存在", _scriptPath);
        }

        var textPath = Path.Combine(_cacheDirectory, $".{Guid.NewGuid():N}.txt");
        await File.WriteAllTextAsync(textPath, text, Encoding.UTF8);
        using var process = new Process
        {
            StartInfo = new ProcessStartInfo
            {
                FileName = _pythonPath,
                UseShellExecute = false,
                CreateNoWindow = true,
                RedirectStandardError = true,
                RedirectStandardOutput = true
            }
        };
        process.StartInfo.ArgumentList.Add(_scriptPath);
        process.StartInfo.ArgumentList.Add("--text-file");
        process.StartInfo.ArgumentList.Add(textPath);
        process.StartInfo.ArgumentList.Add("--output");
        process.StartInfo.ArgumentList.Add(audioPath);

        try
        {
            lock (_gate)
            {
                if (operationId != _operationId)
                {
                    return;
                }
                _generator = process;
            }
            if (!process.Start())
            {
                throw new InvalidOperationException("无法启动Edge TTS生成器");
            }
            var errorTask = process.StandardError.ReadToEndAsync();
            await process.WaitForExitAsync();
            var error = await errorTask;
            if (operationId != Volatile.Read(ref _operationId))
            {
                return;
            }
            if (process.ExitCode != 0 || !File.Exists(audioPath) || new FileInfo(audioPath).Length == 0)
            {
                throw new InvalidOperationException(
                    string.IsNullOrWhiteSpace(error) ? "Edge TTS未生成音频" : error.Trim());
            }
        }
        finally
        {
            lock (_gate)
            {
                if (ReferenceEquals(_generator, process))
                {
                    _generator = null;
                }
            }
            File.Delete(textPath);
        }
    }

    private void StartPlayback(string audioPath, int operationId)
    {
        var player = new MediaPlayer();
        player.MediaEnded += OnPlayerEnded;
        player.MediaFailed += OnPlayerFailed;
        lock (_gate)
        {
            if (operationId != _operationId)
            {
                ClosePlayer(player);
                return;
            }
            _player = player;
        }
        try
        {
            player.Open(new Uri(audioPath, UriKind.Absolute));
            player.Volume = 1.0;
            player.SpeedRatio = 1.3;
            player.Play();
            SetState(SpeechState.Playing);
        }
        catch
        {
            lock (_gate)
            {
                if (ReferenceEquals(_player, player))
                {
                    _player = null;
                }
            }
            ClosePlayer(player);
            throw;
        }
    }

    private void OnPlayerEnded(object? sender, EventArgs e) => CompletePlayback(sender as MediaPlayer);

    private void OnPlayerFailed(object? sender, ExceptionEventArgs e)
    {
        CompletePlayback(sender as MediaPlayer);
    }

    private void CompletePlayback(MediaPlayer? player)
    {
        if (player is null)
        {
            return;
        }
        lock (_gate)
        {
            if (!ReferenceEquals(_player, player))
            {
                return;
            }
            _player = null;
        }
        ClosePlayer(player);
        SetState(SpeechState.Idle);
    }

    private static string ComputeCacheKey(string text)
    {
        var bytes = SHA256.HashData(Encoding.UTF8.GetBytes($"{SettingsVersion}\0{text}"));
        return Convert.ToHexString(bytes).ToLowerInvariant()[..24];
    }

    private void TrimCache()
    {
        try
        {
            foreach (var file in new DirectoryInfo(_cacheDirectory)
                         .EnumerateFiles("message-*.mp3")
                         .OrderByDescending(file => file.LastWriteTimeUtc)
                         .Skip(MaxCachedFiles))
            {
                file.Delete();
            }
        }
        catch (IOException)
        {
            // Cache cleanup must not interrupt playback.
        }
        catch (UnauthorizedAccessException)
        {
            // Cache cleanup must not interrupt playback.
        }
    }

    private static string ResolveExecutable(string preferredPath, string executableName)
    {
        if (File.Exists(preferredPath))
        {
            return preferredPath;
        }
        foreach (var directory in (Environment.GetEnvironmentVariable("PATH") ?? string.Empty)
                     .Split(Path.PathSeparator, StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries))
        {
            var candidate = Path.Combine(directory, executableName);
            if (File.Exists(candidate))
            {
                return candidate;
            }
        }
        throw new FileNotFoundException($"找不到高级TTS依赖：{executableName}");
    }

    private static void StopProcess(Process? process, bool ownsProcess)
    {
        if (process is null)
        {
            return;
        }
        try
        {
            if (!process.HasExited)
            {
                process.Kill(entireProcessTree: true);
            }
        }
        catch (InvalidOperationException)
        {
        }
        finally
        {
            if (ownsProcess)
            {
                process.Dispose();
            }
        }
    }

    private void ClosePlayer(MediaPlayer? player)
    {
        if (player is null)
        {
            return;
        }
        player.MediaEnded -= OnPlayerEnded;
        player.MediaFailed -= OnPlayerFailed;
        player.Stop();
        player.Close();
    }

    private void SetState(SpeechState value)
    {
        if (State == value)
        {
            return;
        }
        State = value;
        IsSpeaking = value != SpeechState.Idle;
        StateChanged?.Invoke(this, EventArgs.Empty);
    }
}

public enum SpeechState
{
    Idle,
    Generating,
    Playing
}
