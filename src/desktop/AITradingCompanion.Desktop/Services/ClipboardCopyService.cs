using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Text;

namespace AITradingCompanion.Desktop.Services;

internal static class ClipboardCopyService
{
    private const int ClipboardCannotOpen = unchecked((int)0x800401D0);

    private sealed class ClipboardBusyException(string message)
        : ExternalException(message, ClipboardCannotOpen);
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

    internal static Task CopyTextAsync(string text, IntPtr ownerWindow) =>
        CopyTextAsync(text, value => NativeClipboard.SetText(ownerWindow, value), Task.Delay);

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

    private static class NativeClipboard
    {
        private const uint UnicodeText = 13;
        private const uint MoveableMemory = 0x0002;

        internal static void SetText(IntPtr ownerWindow, string text)
        {
            if (ownerWindow == IntPtr.Zero)
                throw new ArgumentException("剪贴板所有者窗口尚未就绪。", nameof(ownerWindow));

            var bytes = Encoding.Unicode.GetBytes(text + '\0');
            var memory = GlobalAlloc(MoveableMemory, checked((nuint)bytes.Length));
            if (memory == IntPtr.Zero)
                throw new Win32Exception(Marshal.GetLastWin32Error());

            try
            {
                CopyToGlobalMemory(memory, bytes);
                if (!OpenClipboard(ownerWindow))
                    throw new ClipboardBusyException("OpenClipboard 失败。");

                var clipboardClosed = false;
                try
                {
                    if (!EmptyClipboard())
                        throw new Win32Exception(Marshal.GetLastWin32Error());

                    if (SetClipboardData(UnicodeText, memory) == IntPtr.Zero)
                        throw new Win32Exception(Marshal.GetLastWin32Error());

                    memory = IntPtr.Zero;
                }
                finally
                {
                    clipboardClosed = CloseClipboard();
                }

                if (!clipboardClosed)
                    throw new Win32Exception(Marshal.GetLastWin32Error());
            }
            finally
            {
                if (memory != IntPtr.Zero)
                    _ = GlobalFree(memory);
            }
        }

        private static void CopyToGlobalMemory(IntPtr memory, byte[] bytes)
        {
            var destination = GlobalLock(memory);
            if (destination == IntPtr.Zero)
                throw new Win32Exception(Marshal.GetLastWin32Error());

            try
            {
                Marshal.Copy(bytes, 0, destination, bytes.Length);
            }
            finally
            {
                _ = GlobalUnlock(memory);
            }
        }

        [DllImport("user32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool OpenClipboard(IntPtr ownerWindow);

        [DllImport("user32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool EmptyClipboard();

        [DllImport("user32.dll", SetLastError = true)]
        private static extern IntPtr SetClipboardData(uint format, IntPtr memory);

        [DllImport("user32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool CloseClipboard();

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern IntPtr GlobalAlloc(uint flags, nuint bytes);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern IntPtr GlobalLock(IntPtr memory);

        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool GlobalUnlock(IntPtr memory);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern IntPtr GlobalFree(IntPtr memory);
    }
}
