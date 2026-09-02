using System.Runtime.InteropServices;
using AITradingCompanion.Desktop.Services;

namespace AITradingCompanion.Tests;

public sealed class ClipboardCopyServiceTests
{
    [Fact]
    public async Task RetriesWhenOpenClipboardIsTemporarilyUnavailable()
    {
        var attempts = 0;
        var delays = new List<TimeSpan>();

        await ClipboardCopyService.CopyTextAsync(
            "message",
            _ =>
            {
                attempts++;
                if (attempts < 3)
                    throw Marshal.GetExceptionForHR(unchecked((int)0x800401D0))!;
            },
            duration =>
            {
                delays.Add(duration);
                return Task.CompletedTask;
            });

        Assert.Equal(3, attempts);
        Assert.Equal([TimeSpan.FromMilliseconds(50), TimeSpan.FromMilliseconds(100)], delays);
    }

    [Fact]
    public async Task RecoversWhenClipboardContentionOutlastsTheOriginalRetryWindow()
    {
        var attempts = 0;

        await ClipboardCopyService.CopyTextAsync(
            "message",
            _ =>
            {
                attempts++;
                if (attempts <= 10)
                    throw Marshal.GetExceptionForHR(unchecked((int)0x800401D0))!;
            },
            _ => Task.CompletedTask);

        Assert.Equal(11, attempts);
    }

    [Fact]
    public async Task DoesNotRetryUnrelatedClipboardFailures()
    {
        var attempts = 0;
        var exception = await Assert.ThrowsAsync<COMException>(() => ClipboardCopyService.CopyTextAsync(
            "message",
            _ =>
            {
                attempts++;
                throw Marshal.GetExceptionForHR(unchecked((int)0x80004005))!;
            },
            _ => Task.CompletedTask));

        Assert.Equal(unchecked((int)0x80004005), exception.HResult);
        Assert.Equal(1, attempts);
    }

    [Fact]
    public async Task StopsAfterTheBoundedClipboardBusyRetryWindow()
    {
        var attempts = 0;
        var delays = 0;
        var exception = await Assert.ThrowsAsync<COMException>(() => ClipboardCopyService.CopyTextAsync(
            "message",
            _ =>
            {
                attempts++;
                throw Marshal.GetExceptionForHR(unchecked((int)0x800401D0))!;
            },
            _ =>
            {
                delays++;
                return Task.CompletedTask;
            }));

        Assert.Equal(unchecked((int)0x800401D0), exception.HResult);
        Assert.Equal(21, attempts);
        Assert.Equal(20, delays);
    }
}
