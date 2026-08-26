using System.Runtime.InteropServices;
using System.Text;

namespace AITradingCompanion.Desktop.Services;

internal static class WindowsCredentialStore
{
    private const int GenericCredential = 1;
    private const int EnterprisePersistence = 2;

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    private struct Credential
    {
        public int Flags;
        public int Type;
        public string TargetName;
        public string Comment;
        public System.Runtime.InteropServices.ComTypes.FILETIME LastWritten;
        public int CredentialBlobSize;
        public IntPtr CredentialBlob;
        public int Persist;
        public int AttributeCount;
        public IntPtr Attributes;
        public string TargetAlias;
        public string UserName;
    }

    [DllImport("Advapi32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern bool CredWrite(ref Credential credential, int flags);

    public static void Write(string target, string secret)
    {
        if (string.IsNullOrWhiteSpace(target) || string.IsNullOrWhiteSpace(secret))
            throw new InvalidOperationException("Provider URL and API key are required.");
        var bytes = Encoding.Unicode.GetBytes(secret);
        var blob = Marshal.AllocCoTaskMem(bytes.Length);
        try
        {
            Marshal.Copy(bytes, 0, blob, bytes.Length);
            var credential = new Credential
            {
                Type = GenericCredential,
                TargetName = target,
                CredentialBlobSize = bytes.Length,
                CredentialBlob = blob,
                Persist = EnterprisePersistence,
                UserName = "AITradingCompanion",
            };
            if (!CredWrite(ref credential, 0))
                throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error(), "无法写入 Windows 凭据库。");
        }
        finally { Marshal.FreeCoTaskMem(blob); }
    }
}
