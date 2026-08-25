using NAudio.Wave;

namespace AITradingCompanion.Desktop.Services;

/// <summary>Records the default microphone and exposes normalized PCM peaks for the editable-draft waveform.</summary>
public sealed class VoiceRecordingService : IDisposable
{
    private WaveInEvent? _capture;
    private WaveFileWriter? _writer;
    private string? _outputPath;
    private bool _disposed;

    public event EventHandler<double>? LevelChanged;

    public bool IsRecording => _capture is not null;

    public void Start(string outputPath)
    {
        ObjectDisposedException.ThrowIf(_disposed, this);
        if (IsRecording) throw new InvalidOperationException("Recording is already in progress.");
        Directory.CreateDirectory(Path.GetDirectoryName(outputPath)!);
        _outputPath = outputPath;
        _capture = new WaveInEvent
        {
            DeviceNumber = 0,
            WaveFormat = new WaveFormat(16000, 16, 1),
            BufferMilliseconds = 40,
        };
        _writer = new WaveFileWriter(outputPath, _capture.WaveFormat);
        _capture.DataAvailable += OnDataAvailable;
        _capture.RecordingStopped += OnRecordingStopped;
        try { _capture.StartRecording(); }
        catch { CleanupCapture(); throw; }
    }

    public string StopAndSave()
    {
        ObjectDisposedException.ThrowIf(_disposed, this);
        if (_capture is null || _outputPath is null) throw new InvalidOperationException("Recording is not in progress.");
        var path = _outputPath;
        _capture.StopRecording();
        CleanupCapture();
        LevelChanged?.Invoke(this, 0);
        return path;
    }

    private void OnDataAvailable(object? sender, WaveInEventArgs e)
    {
        _writer?.Write(e.Buffer, 0, e.BytesRecorded);
        var peak = 0d;
        for (var offset = 0; offset + 1 < e.BytesRecorded; offset += 2)
        {
            var sample = Math.Abs(BitConverter.ToInt16(e.Buffer, offset) / 32768d);
            if (sample > peak) peak = sample;
        }
        LevelChanged?.Invoke(this, Math.Clamp(peak, 0, 1));
    }

    private void OnRecordingStopped(object? sender, StoppedEventArgs e) => _writer?.Flush();

    private void CleanupCapture()
    {
        if (_capture is not null)
        {
            _capture.DataAvailable -= OnDataAvailable;
            _capture.RecordingStopped -= OnRecordingStopped;
            _capture.Dispose();
            _capture = null;
        }
        _writer?.Dispose();
        _writer = null;
        _outputPath = null;
    }

    public void Dispose()
    {
        if (_disposed) return;
        if (_capture is not null)
        {
            try { _capture.StopRecording(); }
            catch { }
        }
        CleanupCapture();
        _disposed = true;
        GC.SuppressFinalize(this);
    }
}
