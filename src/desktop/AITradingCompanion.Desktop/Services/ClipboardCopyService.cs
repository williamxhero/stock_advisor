using Clipboard = System.Windows.Clipboard;
using System.Runtime.InteropServices;

namespace AITradingCompanion.Desktop.Services;

internal static class ClipboardCopyService
{
    private const int ClipboardCannotOpen = unchecked((int)0x800401D0);
    private static readonly TimeSpan[] RetryDelays =
    [
        TimeSpan.FromMilliseconds(50),
        TimeSpan.FromMilliseconds(100),
        TimeSpan.FromMilliseconds(200),
        TimeSpan.FromMilliseconds(400),
        TimeSpan.FromMilliseconds(500),
        TimeSpan.FromMilliseconds(500),
        TimeSpan.FromMilliseconds(500),
        TimeSpan.FromMilliseconds(500),
        TimeSpan.FromMilliseconds(500),
        TimeSpan.FromMilliseconds(500),
        TimeSpan.FromMilliseconds(500),
        TimeSpan.FromMilliseconds(500),
        TimeSpan.FromMilliseconds(500),
        TimeSpan.FromMilliseconds(500),
        TimeSpan.FromMilliseconds(500),
        TimeSpan.FromMilliseconds(500),
        TimeSpan.FromMilliseconds(500),
        TimeSpan.FromMilliseconds(500),
        TimeSpan.FromMilliseconds(500),
        TimeSpan.FromMilliseconds(500),
    ];

    internal static Task CopyTextAsync(string text) =>
        CopyTextAsync(text, Clipboard.SetText, Task.Delay);

    internal static async Task CopyTextAsync(
        string text,
        Action<string> writeText,
        Func<TimeSpan, Task> delay)
    {
        for (var attempt = 0; ; attempt++)
        {
            try
            {
                writeText(text);
                return;
            }
            catch (ExternalException exception)
                when (exception.HResult == ClipboardCannotOpen && attempt < RetryDelays.Length)
            {
                await delay(RetryDelays[attempt]);
            }
        }
    }
}
