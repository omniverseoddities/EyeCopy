// Hidden Shell notification receiver for exact registered transfer tickets.
// --folders is a read-only diagnostic; production does not depend on tab lists.
// No window navigation, input synthesis, clipboard access, or recursive search.
using System;
using System.Collections.Generic;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;
using System.Windows.Forms;
using System.Web.Script.Serialization;
using Microsoft.Win32.SafeHandles;

internal static class MarkerDropProbe {
    private static readonly JavaScriptSerializer Json = new JavaScriptSerializer();
    // Match only exact current tickets or already owned legacy nonce tickets.
    private static readonly Regex MarkerName = new Regex(@"^_(EyeCopy)_drop_([0-9a-f]{32})\.(transfer|txt)$", RegexOptions.CultureInvariant);
    private static byte[] ExpectedPayload(string name) {
        Match match = MarkerName.Match(name); if (!match.Success) throw new ArgumentException("Invalid marker name");
        return match.Groups[3].Value == "transfer" ? new byte[0] : Encoding.ASCII.GetBytes(match.Groups[1].Value + " transfer ticket v1\n" + match.Groups[2].Value + "\n");
    }
    private static volatile bool Stop;
    private static readonly object OutputLock = new object(), NamesLock = new object();
    private static readonly HashSet<string> Names = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
    private static void Emit(object value) { lock (OutputLock) { Console.WriteLine(Json.Serialize(value)); Console.Out.Flush(); } }
    private static void Release(object value) { if (value != null && Marshal.IsComObject(value)) Marshal.FinalReleaseComObject(value); }
    private static bool LocalFolder(string path) {
        if (String.IsNullOrEmpty(path) || !Regex.IsMatch(path, @"^[a-zA-Z]:\\") || !Directory.Exists(path)) return false;
        try { DriveType type = new DriveInfo(Path.GetPathRoot(path)).DriveType; return type == DriveType.Fixed || type == DriveType.Removable; }
        catch { return false; }
    }
    private static string[] Folders(object shellObject) {
        HashSet<string> paths = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        object windowsObject = null;
        try {
            dynamic shell = shellObject;
            windowsObject = shell.Windows();
            dynamic windows = windowsObject;
            int count = Math.Min((int)windows.Count, 64);
            for (int i = 0; i < count; i++) {
                object window = null, document = null, folder = null, self = null;
                try {
                    window = windows.Item(i);
                    dynamic view = window;
                    // Ignore IE, hosted HTML and virtual Shell locations.
                    if (!String.Equals(Path.GetFileName((string)view.FullName), "explorer.exe", StringComparison.OrdinalIgnoreCase)) continue;
                    document = view.Document;
                    dynamic doc = document;
                    folder = doc.Folder;
                    dynamic shellFolder = folder;
                    self = shellFolder.Self;
                    dynamic item = self;
                    string path = (string)item.Path;
                    if (LocalFolder(path)) paths.Add(Path.GetFullPath(path));
                } catch { /* Navigating and non-filesystem views are expected. */ }
                finally { Release(self); Release(folder); Release(document); Release(window); }
            }
        } finally { Release(windowsObject); }
        string[] result = new string[paths.Count]; paths.CopyTo(result); Array.Sort(result, StringComparer.OrdinalIgnoreCase); return result;
    }
    private static int Claim(string folder, string name) {
        if (!LocalFolder(folder) || !MarkerName.IsMatch(name) || Path.GetFileName(name) != name) throw new ArgumentException("Invalid marker claim");
        string target = Path.Combine(Path.GetFullPath(folder), name);
        byte[] expected = ExpectedPayload(name);
        bool claimed = false, deleteNotified = false, directoryNotified = false;
        IntPtr filePidl = IntPtr.Zero, folderPidl = IntPtr.Zero;
        int initialized = CoInitializeEx(IntPtr.Zero, 2);
        bool comAvailable = initialized >= 0 || initialized == unchecked((int)0x80010106);
        try {
            if ((File.GetAttributes(target) & (FileAttributes.Directory | FileAttributes.ReparsePoint)) != 0) throw new IOException("Marker is not a normal file");
            // Exclusive sharing makes a still-running Explorer copy retry. Only
            // this exact owned payload is deleted, atomically on handle close.
            using (SafeFileHandle handle = CreateFile(target, 0xC0010000, 0, IntPtr.Zero, 3, 0x00200000, IntPtr.Zero)) {
                if (handle.IsInvalid) throw new IOException("Marker unavailable");
                BY_HANDLE_FILE_INFORMATION metadata;
                if (!GetFileInformationByHandle(handle, out metadata) || metadata.FileSizeHigh != 0 || metadata.FileSizeLow != expected.Length || (metadata.FileAttributes & 0x410) != 0)
                    throw new IOException("Marker size or attributes changed");
                if (expected.Length != 0) {
                    byte[] actual = new byte[expected.Length]; uint read;
                    if (!ReadFile(handle, actual, (uint)actual.Length, out read, IntPtr.Zero) || read != actual.Length) throw new IOException("Marker unavailable");
                    for (int index = 0; index < actual.Length; index++) if (actual[index] != expected[index]) throw new IOException("Marker has unexpected content");
                }
                // Capture the exact existing item PIDL before deleting it.
                // Notification never broadens the verified nonce claim.
                if (comAvailable) filePidl = NotificationPidl(target);
                // Windows allows deleting our open file using a handle, avoiding
                // a close/delete race that could remove a replacement file.
                FILE_DISPOSITION_INFO info = new FILE_DISPOSITION_INFO { DeleteFile = true };
                if (!SetFileInformationByHandle(handle.DangerousGetHandle(), 4, ref info, (uint)Marshal.SizeOf(typeof(FILE_DISPOSITION_INFO)))) throw new IOException("Marker cannot be consumed");
            }
            // The delete-on-close handle has closed before the Shell is told
            // the item is gone, preventing a stale ticket in Explorer's view.
            claimed = true;
        } catch (IOException) { /* Missing, locked and mismatched candidates are not ours to consume. */ }
        catch (UnauthorizedAccessException) { }
        finally {
            try {
                if (claimed) {
                    deleteNotified = NotifyOwnedChange(4, filePidl, target);
                    if (comAvailable) folderPidl = NotificationPidl(Path.GetDirectoryName(target));
                    directoryNotified = NotifyOwnedChange(0x1000, folderPidl, Path.GetDirectoryName(target));
                }
            } catch { /* A refresh failure must not change an already consumed claim. */ }
            finally {
                if (filePidl != IntPtr.Zero) Marshal.FreeCoTaskMem(filePidl);
                if (folderPidl != IntPtr.Zero) Marshal.FreeCoTaskMem(folderPidl);
                if (initialized >= 0) CoUninitialize();
            }
        }
        Emit(new { claimed = claimed, shellDeleteNotificationSent = deleteNotified, shellDirectoryNotificationSent = directoryNotified }); return 0;
    }
    private static IntPtr NotificationPidl(string path) {
        IntPtr pidl = IntPtr.Zero;
        try {
            int result = SHParseDisplayName(path, IntPtr.Zero, out pidl, 0, IntPtr.Zero);
            if (result >= 0) return pidl;
        } catch { /* Optional refresh metadata must not prevent a verified claim. */ }
        if (pidl != IntPtr.Zero) Marshal.FreeCoTaskMem(pidl);
        return IntPtr.Zero;
    }
    private static bool NotifyOwnedChange(int shellEvent, IntPtr pidl, string path) {
        // SHCNF_FLUSHNOWAIT includes FLUSH and returns when delivery begins.
        // PIDLs avoid MAX_PATH/ANSI loss. The Unicode fallback is bounded;
        // an unsupported long path is skipped rather than truncated.
        if (pidl != IntPtr.Zero) { SHChangeNotify(shellEvent, 0x3000, pidl, IntPtr.Zero); return true; }
        if (String.IsNullOrEmpty(path) || path.Length >= 260) return false;
        IntPtr text = Marshal.StringToHGlobalUni(path);
        try { SHChangeNotify(shellEvent, 0x3005, text, IntPtr.Zero); return true; }
        finally { Marshal.FreeHGlobal(text); }
    }
    [DllImport("ole32.dll")] private static extern int CoInitializeEx(IntPtr reserved, uint mode);
    [DllImport("ole32.dll")] private static extern void CoUninitialize();
    [DllImport("shell32.dll", CharSet = CharSet.Unicode)] private static extern int SHParseDisplayName(string path, IntPtr context, out IntPtr pidl, uint attributes, IntPtr attributeOutput);
    [DllImport("shell32.dll")] private static extern void SHChangeNotify(int shellEvent, uint flags, IntPtr first, IntPtr second);
    [StructLayout(LayoutKind.Sequential)] private struct FILE_DISPOSITION_INFO { [MarshalAs(UnmanagedType.U1)] internal bool DeleteFile; }
    [StructLayout(LayoutKind.Sequential)] private struct BY_HANDLE_FILE_INFORMATION {
        internal uint FileAttributes, CreationLow, CreationHigh, AccessLow, AccessHigh, WriteLow, WriteHigh, VolumeSerialNumber, FileSizeHigh, FileSizeLow, NumberOfLinks, FileIndexHigh, FileIndexLow;
    }
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)] private static extern SafeFileHandle CreateFile(string name, uint access, uint sharing, IntPtr security, uint creation, uint attributes, IntPtr template);
    [DllImport("kernel32.dll", SetLastError = true)] private static extern bool GetFileInformationByHandle(SafeFileHandle file, out BY_HANDLE_FILE_INFORMATION info);
    [DllImport("kernel32.dll", SetLastError = true)] private static extern bool ReadFile(SafeFileHandle file, [Out] byte[] bytes, uint count, out uint read, IntPtr overlapped);
    [DllImport("kernel32.dll", SetLastError = true)] private static extern bool SetFileInformationByHandle(IntPtr file, int infoClass, ref FILE_DISPOSITION_INFO info, uint size);
    [StructLayout(LayoutKind.Sequential)] private struct SHChangeNotifyEntry { internal IntPtr pidl; [MarshalAs(UnmanagedType.Bool)] internal bool recursive; }
    [DllImport("shell32.dll")] private static extern int SHGetSpecialFolderLocation(IntPtr owner, int folder, out IntPtr pidl);
    [DllImport("shell32.dll")] private static extern uint SHChangeNotifyRegister(IntPtr window, int sources, int events, uint message, int entries, ref SHChangeNotifyEntry entry);
    [DllImport("shell32.dll")] [return: MarshalAs(UnmanagedType.Bool)] private static extern bool SHChangeNotifyDeregister(uint id);
    [DllImport("shell32.dll")] private static extern IntPtr SHChangeNotification_Lock(IntPtr change, uint processId, out IntPtr pidls, out int shellEvent);
    [DllImport("shell32.dll")] [return: MarshalAs(UnmanagedType.Bool)] private static extern bool SHChangeNotification_Unlock(IntPtr locked);
    [DllImport("shell32.dll", CharSet = CharSet.Unicode)] [return: MarshalAs(UnmanagedType.Bool)] private static extern bool SHGetPathFromIDListEx(IntPtr pidl, StringBuilder path, uint count, uint options);
    private sealed class NotificationWindow : NativeWindow, IDisposable {
        private const int ShellMessage = 0x8001;
        private uint registration;
        private IntPtr desktop;
        internal NotificationWindow() {
            CreateParams parameters = new CreateParams(); parameters.Caption = "EyeCopy marker notifications"; parameters.Parent = new IntPtr(-3);
            CreateHandle(parameters);
            Marshal.ThrowExceptionForHR(SHGetSpecialFolderLocation(IntPtr.Zero, 0, out desktop));
            SHChangeNotifyEntry entry = new SHChangeNotifyEntry { pidl = desktop, recursive = true };
            // Shell-level only. This receives the Shell copy result without
            // installing recursive filesystem watchers across the machine.
            registration = SHChangeNotifyRegister(Handle, 0x8002, 1 | 2 | 0x1000 | 0x2000, ShellMessage, 1, ref entry);
            if (registration == 0) throw new IOException("Shell notification registration failed");
        }
        private static string ReadPath(IntPtr pidl) {
            if (pidl == IntPtr.Zero) return null;
            StringBuilder path = new StringBuilder(32768);
            return SHGetPathFromIDListEx(pidl, path, 32768, 0) ? path.ToString() : null;
        }
        private static void Candidate(string path, int shellEvent) {
            if (String.IsNullOrEmpty(path)) return;
            string name = Path.GetFileName(path);
            lock (NamesLock) { if (!Names.Contains(name)) return; }
            string folder = Path.GetDirectoryName(path);
            if (LocalFolder(folder)) Emit(new { @event = "candidate", basename = name, destination = Path.GetFullPath(folder), shellEvent = shellEvent });
        }
        protected override void WndProc(ref Message message) {
            if (message.Msg == ShellMessage) {
                IntPtr pointers; int shellEvent;
                IntPtr locked = SHChangeNotification_Lock(message.WParam, unchecked((uint)message.LParam.ToInt64()), out pointers, out shellEvent);
                if (locked != IntPtr.Zero) {
                    string first = null, second = null;
                    try {
                        if (pointers != IntPtr.Zero) {
                            first = ReadPath(Marshal.ReadIntPtr(pointers, 0));
                            if ((shellEvent & 1) != 0) second = ReadPath(Marshal.ReadIntPtr(pointers, IntPtr.Size));
                        }
                    } catch { }
                    finally { SHChangeNotification_Unlock(locked); }
                    // Copy strings out while locked; PIDL memory belongs to the
                    // Shell and must never be retained across the unlock.
                    try {
                        if ((shellEvent & 1) != 0) Candidate(second, shellEvent);
                        else if ((shellEvent & (2 | 0x2000)) != 0) Candidate(first, shellEvent);
                        else if ((shellEvent & 0x1000) != 0 && LocalFolder(first)) {
                            string[] names; lock (NamesLock) { names = new string[Names.Count]; Names.CopyTo(names); }
                            // Coalesced notifications identify only one changed
                            // directory. Probe our <=48 exact nonce paths there.
                            foreach (string name in names) { string path = Path.Combine(first, name); if (File.Exists(path)) Candidate(path, shellEvent); }
                        }
                    } catch { }
                }
                message.Result = IntPtr.Zero; return;
            }
            base.WndProc(ref message);
        }
        public void Dispose() {
            if (registration != 0) { SHChangeNotifyDeregister(registration); registration = 0; }
            if (desktop != IntPtr.Zero) { Marshal.FreeCoTaskMem(desktop); desktop = IntPtr.Zero; }
            DestroyHandle();
        }
    }
    private static void ReadCommands() {
        try {
            string line;
            while ((line = Console.ReadLine()) != null) {
                if (line.Length > 4096) throw new ArgumentException("Invalid command");
                Dictionary<string, object> command = new JavaScriptSerializer().Deserialize<Dictionary<string, object>>(line);
                object actionValue, nameValue; string action = command.TryGetValue("command", out actionValue) ? actionValue as string : null;
                if (action == "quit") break;
                string name = command.TryGetValue("basename", out nameValue) ? nameValue as string : null;
                if (name == null || !MarkerName.IsMatch(name)) throw new ArgumentException("Invalid marker name");
                if (action == "register-marker") {
                    bool accepted; int count;
                    lock (NamesLock) { accepted = Names.Contains(name) || Names.Count < 48; if (accepted) Names.Add(name); count = Names.Count; }
                    Emit(new { @event = "marker-registered", basename = name, accepted = accepted, registeredCount = count });
                } else if (action == "unregister-marker") { lock (NamesLock) Names.Remove(name); }
                else throw new ArgumentException("Invalid command");
            }
        } catch (Exception error) { Emit(new { @event = "error", errorType = error.GetType().Name }); }
        finally { Stop = true; }
    }
    [STAThread] private static int Main(string[] args) {
        Console.OutputEncoding = new UTF8Encoding(false);
        object shell = null;
        try {
            if (args.Length == 3 && args[0] == "--claim") return Claim(args[1], args[2]);
            if (args.Length != 1 || (args[0] != "--folders" && args[0] != "--watch")) throw new ArgumentException("Invalid probe mode");
            if (args[0] == "--folders") { shell = Activator.CreateInstance(Type.GetTypeFromProgID("Shell.Application", true)); Emit(new { @event = "folders", folders = Folders(shell) }); return 0; }
            using (NotificationWindow window = new NotificationWindow()) {
                Thread input = new Thread(ReadCommands); input.IsBackground = true; input.Start();
                Emit(new { @event = "ready", notifications = true });
                while (!Stop) { Application.DoEvents(); Thread.Sleep(10); }
            }
            return 0;
        } catch (Exception error) { Emit(new { @event = "error", errorType = error.GetType().Name }); return 1; }
        finally { Release(shell); }
    }
}
