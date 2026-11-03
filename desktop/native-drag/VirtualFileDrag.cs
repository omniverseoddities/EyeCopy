// Uses the Windows Shell's documented virtual-file protocol. Compiles with the
// .NET Framework compiler already included with Windows; no SDK or UAC needed.
using System;
using System.Collections.Generic;
using System.Collections.Concurrent;
using System.Diagnostics;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.Globalization;
using System.IO;
using System.Net;
using System.Runtime.InteropServices;
using System.Runtime.InteropServices.ComTypes;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;
using System.Web.Script.Serialization;
using System.Windows.Forms;
using DataObject = System.Runtime.InteropServices.ComTypes.IDataObject;
using STATSTG = System.Runtime.InteropServices.ComTypes.STATSTG;

namespace EyeCopyNativeDrag {
    public class RemoteFile { public string name; public long size; public string url; public object modified; }
    public class Command { public string command; public string id; public RemoteFile[] files; public string sourceWindow; public uint sourcePid; public bool succeeded; }

    internal static class Native {
        internal const int S_OK = 0, S_FALSE = 1, E_NOTIMPL = unchecked((int)0x80004001);
        internal const int E_ACCESSDENIED = unchecked((int)0x80070005);
        internal const int DV_E_FORMATETC = unchecked((int)0x80040064), DV_E_TYMED = unchecked((int)0x80040069);
        internal const int DV_E_LINDEX = unchecked((int)0x80040068), DV_E_DVASPECT = unchecked((int)0x8004006B);
        internal const int STG_E_READFAULT = unchecked((int)0x8003001E), STG_E_INVALIDFUNCTION = unchecked((int)0x80030001);
        internal const int DRAGDROP_S_DROP = 0x40100, DRAGDROP_S_CANCEL = 0x40101, DRAGDROP_S_USEDEFAULTCURSORS = 0x40102;
        [DllImport("ole32.dll")] internal static extern int OleInitialize(IntPtr reserved);
        [DllImport("ole32.dll")] internal static extern void OleUninitialize();
        [DllImport("ole32.dll")] internal static extern int DoDragDrop([MarshalAs(UnmanagedType.Interface)] DataObject data, [MarshalAs(UnmanagedType.Interface)] IDropSource source, uint effects, out uint effect);
        [DllImport("ole32.dll")] internal static extern void ReleaseStgMedium(ref STGMEDIUM medium);
        [DllImport("ole32.dll")] internal static extern int CreateStreamOnHGlobal(IntPtr memory, [MarshalAs(UnmanagedType.Bool)] bool deleteOnRelease, out IStream stream);
        [DllImport("ole32.dll")] internal static extern int CoMarshalInterface(IStream stream, ref Guid iid, [MarshalAs(UnmanagedType.Interface)] DataObject data, uint context, IntPtr destination, uint flags);
        [DllImport("ole32.dll")] internal static extern int CoUnmarshalInterface(IStream stream, ref Guid iid, out IntPtr data);
        [DllImport("ole32.dll")] internal static extern int CoReleaseMarshalData(IStream stream);
        [DllImport("user32.dll", CharSet=CharSet.Unicode)] internal static extern uint RegisterClipboardFormat(string format);
        [DllImport("user32.dll")] internal static extern short GetAsyncKeyState(int key);
        [DllImport("user32.dll")] internal static extern short GetKeyState(int key);
        [DllImport("user32.dll")] internal static extern bool GetKeyboardState([Out] byte[] keys);
        [DllImport("user32.dll")] internal static extern bool SetKeyboardState(byte[] keys);
        [DllImport("user32.dll")] internal static extern IntPtr GetForegroundWindow();
        [DllImport("user32.dll")] internal static extern IntPtr GetActiveWindow();
        [DllImport("user32.dll")] internal static extern IntPtr GetFocus();
        [DllImport("user32.dll")] internal static extern IntPtr SetFocus(IntPtr window);
        [DllImport("user32.dll")] internal static extern IntPtr GetCapture();
        [DllImport("user32.dll")] internal static extern bool IsWindow(IntPtr window);
        [DllImport("user32.dll")] internal static extern uint GetWindowThreadProcessId(IntPtr window, out uint process);
        [DllImport("user32.dll")] internal static extern bool SetForegroundWindow(IntPtr window);
        [DllImport("user32.dll")] internal static extern bool GetWindowRect(IntPtr window, out WindowRect bounds);
        [DllImport("user32.dll")] internal static extern bool PostThreadMessage(uint thread, uint message, UIntPtr wParam, IntPtr lParam);
        [DllImport("kernel32.dll")] internal static extern uint GetCurrentThreadId();
        [DllImport("kernel32.dll", SetLastError=true)] internal static extern IntPtr GlobalAlloc(uint flags, UIntPtr bytes);
        [DllImport("kernel32.dll")] internal static extern IntPtr GlobalLock(IntPtr memory);
        [DllImport("kernel32.dll")] internal static extern bool GlobalUnlock(IntPtr memory);
        [DllImport("kernel32.dll")] internal static extern IntPtr GlobalFree(IntPtr memory);
        [DllImport("kernel32.dll")] internal static extern UIntPtr GlobalSize(IntPtr memory);
        [DllImport("gdi32.dll")] internal static extern bool DeleteObject(IntPtr value);
        [DllImport("shell32.dll", CharSet=CharSet.Unicode)] internal static extern int SHCreateItemFromParsingName(string path, IntPtr context, ref Guid iid, [MarshalAs(UnmanagedType.Interface)] out IShellItem item);
        [DllImport("shell32.dll")] internal static extern int SHCreateDataObject(IntPtr folder, uint count, IntPtr children, IntPtr inner, ref Guid iid, out IntPtr data);
        internal static short Format(string name) { return unchecked((short)RegisterClipboardFormat(name)); }
        internal static bool CopyEffect(uint effect) { return (effect & 7) == 1; }
        internal static bool CopyDrop(int result, uint effect) { return result == DRAGDROP_S_DROP && CopyEffect(effect); }
        internal static int ContinueDrag(bool escape, bool cancelled, uint keys, bool hardwareHeld, bool trustedGesture) {
            if (escape || cancelled) return DRAGDROP_S_CANCEL;
            // Queued key flags can describe an older mouse-up. For our
            // validated cross-process gesture, the physical button decides
            // when to release, including a queued down after actual release.
            bool held=trustedGesture ? hardwareHeld : (keys & 1) != 0;
            return held ? S_OK : DRAGDROP_S_DROP;
        }
        internal static bool LeftButtonHeld { get { return (GetAsyncKeyState(1) & 0x8000) != 0; } }
        internal static bool ThreadLeftButtonHeld { get { return (GetKeyState(1) & 0x8000) != 0; } }
        internal static void SetThreadLeftButton(bool held) {
            // Only this helper thread's input-state table is changed. Preserve
            // every other key and toggle bit; no input messages are generated.
            byte[] keys=new byte[256];
            if (!GetKeyboardState(keys)) throw new InvalidOperationException("The drag thread key state is unavailable.");
            keys[1]=(byte)((keys[1] & 0x7f) | (held ? 0x80 : 0));
            if (!SetKeyboardState(keys)) throw new InvalidOperationException("The drag thread key state could not be synchronized.");
        }
        internal static bool HelperForeground {
            get { uint process; GetWindowThreadProcessId(GetForegroundWindow(), out process); return process == (uint)Process.GetCurrentProcess().Id; }
        }
        internal static STGMEDIUM HGlobal(byte[] bytes) {
            IntPtr handle = GlobalAlloc(0x42, new UIntPtr((uint)bytes.Length));
            if (handle == IntPtr.Zero) throw new OutOfMemoryException();
            IntPtr ptr = GlobalLock(handle);
            if (ptr == IntPtr.Zero) { GlobalFree(handle); throw new OutOfMemoryException(); }
            try { Marshal.Copy(bytes, 0, ptr, bytes.Length); } finally { GlobalUnlock(handle); }
            return new STGMEDIUM { tymed=TYMED.TYMED_HGLOBAL, unionmember=handle, pUnkForRelease=null };
        }
    }

    [ComVisible(true), Guid("00000121-0000-0000-C000-000000000046"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    public interface IDropSource {
        [PreserveSig] int QueryContinueDrag([MarshalAs(UnmanagedType.Bool)] bool escape, uint keys);
        [PreserveSig] int GiveFeedback(uint effect);
    }
    [ComVisible(true), Guid("3D8B0590-F691-11D2-8EA9-006097DF5BD4"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    public interface IDataObjectAsyncCapability {
        [PreserveSig] int SetAsyncMode([MarshalAs(UnmanagedType.Bool)] bool mode);
        [PreserveSig] int GetAsyncMode([MarshalAs(UnmanagedType.Bool)] out bool mode);
        [PreserveSig] int StartOperation(IntPtr context);
        [PreserveSig] int InOperation([MarshalAs(UnmanagedType.Bool)] out bool active);
        [PreserveSig] int EndOperation(int result, IntPtr context, uint effects);
    }
    // Preserve native HRESULTs so an EOF returns S_FALSE rather than an
    // incorrectly marshalled exception. All offsets and lengths are 64 bit.
    [ComVisible(true), Guid("0000000C-0000-0000-C000-000000000046"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    public interface INativeStream {
        [PreserveSig] int Read(IntPtr buffer, uint count, IntPtr actual);
        [PreserveSig] int Write(IntPtr buffer, uint count, IntPtr actual);
        [PreserveSig] int Seek(long offset, uint origin, IntPtr position);
        [PreserveSig] int SetSize(ulong size);
        [PreserveSig] int CopyTo(IntPtr destination, ulong count, IntPtr read, IntPtr written);
        [PreserveSig] int Commit(uint flags);
        [PreserveSig] int Revert();
        [PreserveSig] int LockRegion(ulong offset, ulong count, uint type);
        [PreserveSig] int UnlockRegion(ulong offset, ulong count, uint type);
        [PreserveSig] int Stat(out STATSTG stat, uint flags);
        [PreserveSig] int Clone(out INativeStream stream);
    }
    [ComImport, Guid("43826D1E-E718-42EE-BC55-A1E261C37BFE"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    internal interface IShellItem {
        [PreserveSig] int BindToHandler(IntPtr context, ref Guid handler, ref Guid iid, out IntPtr result);
    }
    [StructLayout(LayoutKind.Sequential)] internal struct PointL { internal int x, y; }
    [StructLayout(LayoutKind.Sequential)] internal struct WindowRect { internal int left, top, right, bottom; }
    [StructLayout(LayoutKind.Sequential)] internal struct DragImage { internal PointL size, offset; internal IntPtr bitmap; internal uint colorKey; }
    [ComImport, Guid("DE5BF786-477A-11D2-839D-00C04FD918D0"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    internal interface IDragSourceHelper {
        [PreserveSig] int InitializeFromBitmap(ref DragImage image, [MarshalAs(UnmanagedType.Interface)] DataObject data);
        [PreserveSig] int InitializeFromWindow(IntPtr window, ref PointL point, [MarshalAs(UnmanagedType.Interface)] DataObject data);
    }
    [ComImport, Guid("00000122-0000-0000-C000-000000000046"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    internal interface IDropTarget {
        [PreserveSig] int DragEnter([MarshalAs(UnmanagedType.Interface)] DataObject data, uint keys, PointL point, ref uint effect);
        [PreserveSig] int DragOver(uint keys, PointL point, ref uint effect);
        [PreserveSig] int DragLeave();
        [PreserveSig] int Drop([MarshalAs(UnmanagedType.Interface)] DataObject data, uint keys, PointL point, ref uint effect);
    }

    [ComVisible(true), ClassInterface(ClassInterfaceType.None)]
    public sealed class DropSource : IDropSource {
        private readonly VirtualFiles files;
        private bool continued, feedback; private uint lastEffect;
        public DropSource(VirtualFiles files) { this.files=files; }
        public int QueryContinueDrag(bool escape, uint keys) {
            bool hardwareHeld=Native.LeftButtonHeld, threadHeld=Native.ThreadLeftButtonHeld;
            bool trusted=files.SourceWindow != IntPtr.Zero && Object.ReferenceEquals(Program.OleDrag, files);
            bool synchronized=false;
            if (trusted && !escape && !files.Cancelled && hardwareHeld && !threadHeld && (keys & 1) == 0) {
                Native.SetThreadLeftButton(true); files.ThreadButtonAdjusted=true; synchronized=true;
            }
            if (!continued || escape || (keys & 1) == 0 || !hardwareHeld || files.Cancelled) {
                continued=true;
                Program.Emit(new { @event="drag_callback", id=files.Id, stage="continue", escape=escape, keys=keys, hardwareHeld=hardwareHeld, threadButtonHeld=threadHeld, threadButtonSynced=synchronized, cancelled=files.Cancelled, ownCapture=Native.GetCapture() != IntPtr.Zero, helperForeground=Native.HelperForeground });
            }
            return Native.ContinueDrag(escape, files.Cancelled, keys, hardwareHeld, trusted);
        }
        public int GiveFeedback(uint effect) {
            if (!feedback || effect != lastEffect) {
                feedback=true; lastEffect=effect;
                Program.Emit(new { @event="drag_callback", id=files.Id, stage="feedback", effect=effect, ownCapture=Native.GetCapture() != IntPtr.Zero, helperForeground=Native.HelperForeground });
            }
            return Native.DRAGDROP_S_USEDEFAULTCURSORS;
        }
    }

    // OLE's capture window belongs to the thread calling DoDragDrop. Give that
    // thread a foreground window for this explicit drag gesture, rather than
    // starting capture in a permanently invisible background process. This
    // window never appears in the taskbar and cannot activate until the trusted
    // Electron source window is still foreground with the left button held.
    internal sealed class DragHost : Form {
        private readonly IntPtr source;
        protected override bool ShowWithoutActivation { get { return true; } }
        internal static IntPtr Validate(string value, uint process) {
            ulong numeric;
            if (process == 0 || !ulong.TryParse(value, NumberStyles.None, CultureInfo.InvariantCulture, out numeric) || numeric == 0 || numeric > long.MaxValue) throw new ArgumentException("Invalid drag source window.");
            IntPtr window=new IntPtr((long)numeric); uint owner;
            if (!Native.IsWindow(window) || Native.GetWindowThreadProcessId(window, out owner) == 0 || owner != process) throw new ArgumentException("The drag source window does not belong to EyeCopy.");
            return window;
        }
        internal DragHost(IntPtr source) {
            this.source=source;
            FormBorderStyle=FormBorderStyle.None; ShowInTaskbar=false; StartPosition=FormStartPosition.Manual;
            Opacity=0.01; Width=1; Height=1;
            WindowRect bounds;
            if (!Native.GetWindowRect(source, out bounds)) throw new InvalidOperationException("The EyeCopy window is unavailable.");
            Left=bounds.left; Top=bounds.top;
        }
        internal bool Handoff(VirtualFiles files) {
            if (!Native.LeftButtonHeld || Native.GetForegroundWindow() != source) return false;
            Show();
            // Showing this no-activate window must not override a new user
            // focus change between accepting the gesture and creating it.
            if (!Native.LeftButtonHeld || Native.GetForegroundWindow() != source) return false;
            // The foreground Electron parent activates only this verified host.
            // Asking the background child to activate itself is denied by
            // Windows on some systems; a separate grant can expire on a mouse
            // move before the child processes it. The private ACK is bounded.
            Program.Emit(new { @event="foreground_request", id=files.Id, window=Handle.ToInt64().ToString(CultureInfo.InvariantCulture) });
            Stopwatch waiting=Stopwatch.StartNew();
            // A cross-thread SetForegroundWindow can update global foreground
            // before this thread receives activation. Keep pumping until the
            // actual active window belongs to this host; otherwise OLE starts
            // with a background capture and misses movement and mouse-up.
            while ((files.ForegroundAck == 0 || (files.ForegroundAck == 1 && Native.GetActiveWindow() != Handle)) && !files.Finished && Native.LeftButtonHeld && waiting.ElapsedMilliseconds < 1200) {
                IntPtr foreground=Native.GetForegroundWindow();
                if (foreground != source && foreground != Handle) return false;
                Application.DoEvents(); Thread.Sleep(5);
            }
            bool ready=files.ForegroundAck == 1 && !files.Finished && Native.LeftButtonHeld && Native.GetForegroundWindow() == Handle && Native.GetActiveWindow() == Handle;
            if (ready) Native.SetFocus(Handle);
            return ready && Native.GetFocus() == Handle;
        }
        internal void EndGesture() {
            // Restore only our own source's focus, and only if this short-lived
            // helper still owns foreground. Leave an activated Explorer alone.
            uint process;
            if (IsHandleCreated && Native.GetForegroundWindow() == Handle && Native.IsWindow(source) && Native.GetWindowThreadProcessId(source, out process) != 0 && process == Program.SourceProcess) Native.SetForegroundWindow(source);
            Hide();
        }
    }

    internal static class DragVisual {
        internal static IDragSourceHelper Create(VirtualFiles files) {
            IDragSourceHelper helper=null; IntPtr bitmap=IntPtr.Zero;
            try {
                helper=(IDragSourceHelper)Activator.CreateInstance(Type.GetTypeFromCLSID(new Guid("4657278A-411B-11D2-839A-00C04FD918D0")));
                using (Bitmap image=new Bitmap(244, 66)) using (Graphics drawing=Graphics.FromImage(image))
                using (LinearGradientBrush background=new LinearGradientBrush(new Rectangle(0,0,244,66), Color.FromArgb(26,17,20), Color.FromArgb(76,14,29), 0f))
                using (Pen border=new Pen(Color.FromArgb(244,50,87), 2))
                using (SolidBrush text=new SolidBrush(Color.White))
                using (SolidBrush accent=new SolidBrush(Color.FromArgb(255,65,102)))
                using (Font title=new Font("Segoe UI", 10, FontStyle.Bold))
                using (Font caption=new Font("Segoe UI", 8))
                using (StringFormat format=new StringFormat { Trimming=StringTrimming.EllipsisCharacter, FormatFlags=StringFormatFlags.NoWrap }) {
                    drawing.FillRectangle(background, 0,0,244,66); drawing.DrawRectangle(border, 1,1,241,63);
                    drawing.FillRectangle(accent, 12,17,20,30); drawing.DrawLine(Pens.White,16,25,28,25); drawing.DrawLine(Pens.White,16,31,28,31);
                    string name=files.Files.Length == 1 ? files.Files[0].name : files.Files.Length+" files";
                    drawing.DrawString(name, title, text, new RectangleF(42,11,190,24), format);
                    drawing.DrawString("Copy to a folder", caption, text, new RectangleF(42,37,190,20), format);
                    bitmap=image.GetHbitmap();
                }
                DragImage drag=new DragImage { size=new PointL { x=244, y=66 }, offset=new PointL { x=12, y=12 }, bitmap=bitmap, colorKey=0xffffffff };
                int result=helper.InitializeFromBitmap(ref drag, files);
                if (result < 0) Marshal.ThrowExceptionForHR(result);
                // Windows owns and deletes a successfully accepted HBITMAP.
                bitmap=IntPtr.Zero;
                return helper;
            } catch {
                if (bitmap != IntPtr.Zero) Native.DeleteObject(bitmap);
                if (helper != null) Marshal.ReleaseComObject(helper);
                return null;
            }
        }
    }

    [ComVisible(true), ClassInterface(ClassInterfaceType.None)]
    public sealed class FormatEnumerator : IEnumFORMATETC {
        private readonly FORMATETC[] formats; private int position;
        public FormatEnumerator(FORMATETC[] formats, int position) { this.formats=formats; this.position=position; }
        public int Next(int count, FORMATETC[] output, int[] fetched) {
            int copied=0;
            while (copied < count && position < formats.Length) output[copied++]=formats[position++];
            if (fetched != null && fetched.Length != 0) fetched[0]=copied;
            return copied == count ? Native.S_OK : Native.S_FALSE;
        }
        public int Skip(int count) { int remaining=formats.Length-position; position=Math.Min(formats.Length, position+count); return remaining >= count ? Native.S_OK : Native.S_FALSE; }
        public int Reset() { position=0; return Native.S_OK; }
        public void Clone(out IEnumFORMATETC clone) { clone=new FormatEnumerator(formats, position); }
    }

    [ComVisible(true), ClassInterface(ClassInterfaceType.None)]
    public sealed class VirtualFiles : DataObject, IDataObjectAsyncCapability {
        internal static readonly short Descriptor=Native.Format("FileGroupDescriptorW");
        internal static readonly short Contents=Native.Format("FileContents");
        private static readonly short Preferred=Native.Format("Preferred DropEffect"), DragLoop=Native.Format("InShellDragLoop");
        public readonly string Id; public readonly RemoteFile[] Files;
        internal IntPtr SourceWindow;
        internal volatile int ForegroundAck;
        internal volatile bool GestureActive;
        internal bool ThreadButtonAdjusted;
        public volatile bool Cancelled; internal bool InDragLoop=true, AsyncStarted, AsyncMode=true;
        internal bool Finished { get { return finished; } }
        private volatile bool finished; private long networkBytes; private readonly long[] fileBytes;
        private readonly Stopwatch clock=new Stopwatch(); private long lastReport;
        private readonly List<RemoteStream> streams=new List<RemoteStream>();
        private readonly object privateLock=new object(); private DataObject privateData;
        private readonly Dictionary<string, FORMATETC> privateFormats=new Dictionary<string, FORMATETC>();
        private readonly Dictionary<string, long> privateSizes=new Dictionary<string, long>(); private long privateBytes;
        private readonly Dictionary<string, IntPtr> privateOwners=new Dictionary<string, IntPtr>();
        private int cleanupPosted;
        private static string PrivateKey(FORMATETC format) { return format.cfFormat+":"+format.dwAspect+":"+format.lindex; }
        private DataObject PrivateData {
            get {
                if (privateData == null) {
                    Guid iid=new Guid("0000010E-0000-0000-C000-000000000046"); IntPtr pointer;
                    Marshal.ThrowExceptionForHR(Native.SHCreateDataObject(IntPtr.Zero, 0, IntPtr.Zero, IntPtr.Zero, ref iid, out pointer));
                    privateData=(DataObject)Marshal.GetObjectForIUnknown(pointer); Marshal.Release(pointer);
                }
                return privateData;
            }
        }
        internal int PrivateFormatCount { get { lock (privateLock) return privateFormats.Count; } }
        internal void ReleasePrivateData() {
            lock (privateLock) {
                if (privateData != null) Marshal.ReleaseComObject(privateData);
                foreach (IntPtr owner in privateOwners.Values) Marshal.Release(owner);
                privateData=null; privateFormats.Clear(); privateSizes.Clear(); privateBytes=0;
                privateOwners.Clear();
            }
        }
        internal void ReleasePrivateWhenReady() {
            if (!finished || GestureActive) return;
            if (Native.GetCurrentThreadId() == Program.StaThread) { ReleasePrivateData(); return; }
            if (Interlocked.CompareExchange(ref cleanupPosted, 1, 0) != 0) return;
            if (!Program.PostToSta(delegate() {
                Interlocked.Exchange(ref cleanupPosted, 0);
                if (finished && !GestureActive) ReleasePrivateData();
            })) Interlocked.Exchange(ref cleanupPosted, 0);
        }
        public VirtualFiles(string id, RemoteFile[] files) { Id=id; Files=files; fileBytes=new long[files.Length]; }
        public static void Validate(RemoteFile file) {
            if (file == null || file.size < 0 || string.IsNullOrEmpty(file.name) || file.name.Length > 240 || file.name != Path.GetFileName(file.name)
                || file.name.IndexOfAny(Path.GetInvalidFileNameChars()) >= 0 || file.name.EndsWith(".") || file.name.EndsWith(" ")
                || Regex.IsMatch(file.name, @"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", RegexOptions.IgnoreCase))
                throw new ArgumentException("A dragged item has an invalid Windows filename or size.");
            Uri uri;
            if (!Uri.TryCreate(file.url, UriKind.Absolute, out uri) || uri.Scheme != "http" || uri.Host != "127.0.0.1" || uri.Port < 1
                || uri.UserInfo.Length != 0 || uri.Query.Length != 0 || uri.Fragment.Length != 0
                || !Regex.IsMatch(uri.AbsolutePath, @"^/[A-Za-z0-9_-]{20,128}/original$"))
                throw new ArgumentException("The drag source must be an authenticated local EyeCopy stream.");
        }
        public byte[] Descriptors() {
            // FILEGROUPDESCRIPTORW: DWORD count, then packed 592-byte
            // FILEDESCRIPTORW records. Name at 72, size high/low at 64/68.
            using (MemoryStream memory=new MemoryStream()) using (BinaryWriter writer=new BinaryWriter(memory, Encoding.Unicode)) {
                writer.Write(Files.Length);
                foreach (RemoteFile file in Files) {
                    long start=memory.Position;
                    writer.Write(unchecked((uint)0x80004064));
                    writer.Write(new byte[32]); writer.Write((uint)0x80);
                    writer.Write((long)0); writer.Write((long)0);
                    DateTime modified; long fileTime=0;
                    if (file.modified != null) {
                        double seconds;
                        if (double.TryParse(Convert.ToString(file.modified, CultureInfo.InvariantCulture), NumberStyles.Float, CultureInfo.InvariantCulture, out seconds)) {
                            try { fileTime=new DateTime(1970,1,1,0,0,0,DateTimeKind.Utc).AddSeconds(seconds).ToFileTimeUtc(); } catch { }
                        } else if (DateTime.TryParse(Convert.ToString(file.modified), CultureInfo.InvariantCulture, DateTimeStyles.AssumeUniversal | DateTimeStyles.AdjustToUniversal, out modified)) {
                            try { fileTime=modified.ToFileTimeUtc(); } catch { }
                        }
                    }
                    if (fileTime == 0) fileTime=DateTime.UtcNow.ToFileTimeUtc();
                    writer.Write(fileTime); writer.Write((uint)((ulong)file.size >> 32)); writer.Write((uint)((ulong)file.size & 0xffffffff));
                    writer.Write(Encoding.Unicode.GetBytes(file.name)); writer.Write(new byte[520-Encoding.Unicode.GetByteCount(file.name)]);
                    if (memory.Position-start != 592) throw new InvalidOperationException("Incorrect FILEDESCRIPTORW alignment.");
                }
                return memory.ToArray();
            }
        }
        private static FORMATETC Format(short id, TYMED medium, int index) { return new FORMATETC { cfFormat=id, dwAspect=DVASPECT.DVASPECT_CONTENT, lindex=index, ptd=IntPtr.Zero, tymed=medium }; }
        public int QueryGetData(ref FORMATETC format) {
            if (format.dwAspect != DVASPECT.DVASPECT_CONTENT) return Native.DV_E_DVASPECT;
            if (format.cfFormat == Contents) {
                if (format.lindex < -1 || format.lindex >= Files.Length) return Native.DV_E_LINDEX;
                return (format.tymed & TYMED.TYMED_ISTREAM) != 0 ? Native.S_OK : Native.DV_E_TYMED;
            }
            if (format.cfFormat != Descriptor && format.cfFormat != Preferred && format.cfFormat != DragLoop) {
                lock (privateLock) return privateData == null || !privateFormats.ContainsKey(PrivateKey(format)) ? Native.DV_E_FORMATETC : privateData.QueryGetData(ref format);
            }
            return (format.tymed & TYMED.TYMED_HGLOBAL) != 0 ? Native.S_OK : Native.DV_E_TYMED;
        }
        public void GetData(ref FORMATETC format, out STGMEDIUM medium) {
            int hr=QueryGetData(ref format); if (hr != 0) Marshal.ThrowExceptionForHR(hr);
            if (format.cfFormat == Descriptor) { medium=Native.HGlobal(Descriptors()); return; }
            if (format.cfFormat == Preferred) { medium=Native.HGlobal(BitConverter.GetBytes(1)); return; }
            if (format.cfFormat == DragLoop) { medium=Native.HGlobal(BitConverter.GetBytes(InDragLoop ? 1 : 0)); return; }
            if (format.cfFormat != Contents) { lock (privateLock) PrivateData.GetData(ref format, out medium); return; }
            int index=format.lindex;
            if (index == -1 && Files.Length == 1) index=0;
            if (index < 0) Marshal.ThrowExceptionForHR(Native.DV_E_LINDEX);
            RemoteStream stream=new RemoteStream(this, index);
            Register(stream);
            medium=new STGMEDIUM { tymed=TYMED.TYMED_ISTREAM, unionmember=Marshal.GetComInterfaceForObject(stream, typeof(INativeStream)), pUnkForRelease=null };
        }
        public void GetDataHere(ref FORMATETC format, ref STGMEDIUM medium) { Marshal.ThrowExceptionForHR(Native.DV_E_TYMED); }
        public int GetCanonicalFormatEtc(ref FORMATETC input, out FORMATETC output) { output=input; output.ptd=IntPtr.Zero; return 0x40130; }
        public void SetData(ref FORMATETC format, ref STGMEDIUM medium, bool release) {
            // Shell's drag-image manager needs arbitrary private formats and
            // exact STGMEDIUM ownership. Delegate storage/copy/refcounting to a
            // native Shell data object rather than flattening COM context into
            // managed byte arrays. This never accesses the system clipboard.
            if ((format.tymed & medium.tymed) == 0) Marshal.ThrowExceptionForHR(Native.DV_E_TYMED);
            long size=medium.tymed == TYMED.TYMED_HGLOBAL ? checked((long)Native.GlobalSize(medium.unionmember).ToUInt64()) : 0;
            if (size > 8*1024*1024) Marshal.ThrowExceptionForHR(Native.E_ACCESSDENIED);
            string key=PrivateKey(format);
            lock (privateLock) {
                if (finished) { if (release) Native.ReleaseStgMedium(ref medium); return; }
                long previous; privateSizes.TryGetValue(key, out previous);
                if ((!privateFormats.ContainsKey(key) && privateFormats.Count >= 64) || privateBytes-previous+size > 16*1024*1024) Marshal.ThrowExceptionForHR(Native.E_ACCESSDENIED);
                STGMEDIUM owned=medium; IntPtr owner=IntPtr.Zero; bool copied=false;
                try {
                    if (!release) {
                        // The native Shell store requires ownership transfer.
                        // Clone caller-owned media first, retaining a context
                        // owner separately until replacement/cleanup.
                        owned.pUnkForRelease=null;
                        if (medium.tymed == TYMED.TYMED_HGLOBAL) {
                            if (size <= 0) Marshal.ThrowExceptionForHR(Native.DV_E_TYMED);
                            byte[] bytes=new byte[(int)size]; IntPtr pointer=Native.GlobalLock(medium.unionmember);
                            if (pointer == IntPtr.Zero) throw new OutOfMemoryException();
                            try { Marshal.Copy(pointer, bytes, 0, bytes.Length); } finally { Native.GlobalUnlock(medium.unionmember); }
                            owned=Native.HGlobal(bytes);
                        } else if (medium.tymed == TYMED.TYMED_ISTREAM || medium.tymed == TYMED.TYMED_ISTORAGE) {
                            Marshal.AddRef(medium.unionmember);
                        } else Marshal.ThrowExceptionForHR(Native.DV_E_TYMED);
                        copied=true;
                        if (medium.pUnkForRelease != null) owner=Marshal.GetIUnknownForObject(medium.pUnkForRelease);
                    }
                    PrivateData.SetData(ref format, ref owned, true); copied=false;
                    IntPtr previousOwner;
                    if (privateOwners.TryGetValue(key, out previousOwner)) { Marshal.Release(previousOwner); privateOwners.Remove(key); }
                    if (owner != IntPtr.Zero) { privateOwners[key]=owner; owner=IntPtr.Zero; }
                } finally {
                    if (copied) Native.ReleaseStgMedium(ref owned);
                    if (owner != IntPtr.Zero) Marshal.Release(owner);
                }
                FORMATETC stored=format; stored.ptd=IntPtr.Zero;
                privateFormats[key]=stored; privateSizes[key]=size; privateBytes=privateBytes-previous+size;
            }
        }
        public IEnumFORMATETC EnumFormatEtc(DATADIR direction) {
            if (direction != DATADIR.DATADIR_GET) Marshal.ThrowExceptionForHR(Native.E_NOTIMPL);
            List<FORMATETC> formats=new List<FORMATETC>(new[] { Format(Descriptor, TYMED.TYMED_HGLOBAL, -1), Format(Contents, TYMED.TYMED_ISTREAM, -1), Format(Preferred, TYMED.TYMED_HGLOBAL, -1), Format(DragLoop, TYMED.TYMED_HGLOBAL, -1) });
            lock (privateLock) foreach (FORMATETC item in privateFormats.Values) if (item.cfFormat != Descriptor && item.cfFormat != Contents && item.cfFormat != Preferred && item.cfFormat != DragLoop) formats.Add(item);
            return new FormatEnumerator(formats.ToArray(), 0);
        }
        public int DAdvise(ref FORMATETC format, ADVF flags, IAdviseSink sink, out int connection) { connection=0; return unchecked((int)0x80040003); }
        public void DUnadvise(int connection) { }
        public int EnumDAdvise(out IEnumSTATDATA enumerator) { enumerator=null; return unchecked((int)0x80040003); }
        public int SetAsyncMode(bool mode) { AsyncMode=mode; return Native.S_OK; }
        public int GetAsyncMode(out bool mode) { mode=AsyncMode; return Native.S_OK; }
        public int StartOperation(IntPtr context) { AsyncStarted=true; Program.Emit(new { @event="copying", id=Id, total=Total }); return Native.S_OK; }
        public int InOperation(out bool active) { active=AsyncStarted && !finished; return Native.S_OK; }
        public int EndOperation(int result, IntPtr context, uint effects) {
            if (Cancelled) Finish("cancelled", "The copy was cancelled. Check Explorer for a partial file.");
            else if (result < 0) Finish("error", "Explorer could not complete the copy. Check its destination for a partial file before retrying.");
            else if (Native.CopyEffect(effects)) FinishRead();
            else Finish("cancelled", "Explorer cancelled the copy.");
            return Native.S_OK;
        }
        private long Total { get { long total=0; foreach (RemoteFile file in Files) total=checked(total+file.size); return total; } }
        internal void FinishRead() {
            bool allRead=true; lock (fileBytes) for (int n=0; n<Files.Length; n++) if (fileBytes[n] < Files[n].size) allRead=false;
            if (allRead) Finish("completed", null);
            else Finish("cancelled", "Explorer finished without copying every selected file. Some files may have been skipped; check the chosen folder.");
        }
        internal void BeginRead() { lock (fileBytes) if (!clock.IsRunning) clock.Start(); }
        internal void Report(int index, long contiguous, int actual) {
            lock (fileBytes) {
                if (finished) return;
                networkBytes+=actual; fileBytes[index]=Math.Max(fileBytes[index], contiguous);
                long now=clock.ElapsedMilliseconds;
                if (now-lastReport < 150 && contiguous != Files[index].size) return;
                lastReport=now; long bytes=0; foreach (long count in fileBytes) bytes+=count;
                Program.Emit(new { @event="progress", id=Id, index=index, name=Files[index].name, bytes=bytes, total=Total, fileBytes=fileBytes[index], fileTotal=Files[index].size, speedBps=clock.Elapsed.TotalSeconds > 0 ? networkBytes/clock.Elapsed.TotalSeconds : 0 });
            }
        }
        internal void Failure(Exception error) { Finish("error", error is WebException ? "The iPhone stream stopped. Reconnect and retry; Explorer may retain a partial file." : "The dragged file could not be read. Reconnect and retry; Explorer may retain a partial file."); }
        internal void Register(RemoteStream stream) { lock (streams) streams.Add(stream); }
        internal void Finish(string outcome, string message) {
            lock (fileBytes) {
                if (finished) return;
                finished=true; if (outcome != "completed") Cancelled=true;
                Program.Emit(new { @event=outcome, id=Id, message=message, total=Total });
            }
            VirtualFiles ignored; Program.Active.TryRemove(Id, out ignored);
            RemoteStream[] snapshot; lock (streams) snapshot=streams.ToArray();
            // Never wait for Read's lock: cancellation must interrupt a pending
            // HTTP header/body operation, including a blocked COM call on STA.
            foreach (RemoteStream stream in snapshot) stream.AbortConnection();
            ReleasePrivateWhenReady();
        }
        internal void Cancel() {
            Finish("cancelled", "The copy was cancelled. Check Explorer for a partial file.");
            // Wake only this helper's OLE loop, so cancellation does not depend
            // on the user moving the mouse again. No system input is injected.
            if (Object.ReferenceEquals(Program.OleDrag, this)) Native.PostThreadMessage(Program.StaThread, 0x0100, new UIntPtr(0x1B), IntPtr.Zero);
        }
    }

    [ComVisible(true), ClassInterface(ClassInterfaceType.None)]
    public sealed class RemoteStream : INativeStream {
        private readonly VirtualFiles owner; private readonly int index; private readonly RemoteFile file;
        private long position, contiguous; private HttpWebResponse response; private Stream input; private HttpWebRequest activeRequest;
        private readonly object readLock=new object();
        private readonly object connectionLock=new object();
        internal RemoteStream(VirtualFiles owner, int index) { this.owner=owner; this.index=index; file=owner.Files[index]; }
        internal bool HasRequest { get { lock (connectionLock) return activeRequest != null; } }
        internal bool HasInput { get { return input != null; } }
        internal void AbortConnection() {
            HttpWebRequest request; lock (connectionLock) request=activeRequest;
            if (request != null) { try { request.Abort(); } catch { } }
        }
        internal void CloseConnection() {
            Stream oldInput; HttpWebResponse oldResponse;
            lock (connectionLock) { oldInput=input; oldResponse=response; input=null; response=null; activeRequest=null; }
            if (oldInput != null) oldInput.Dispose();
            if (oldResponse != null) oldResponse.Dispose();
        }
        private void Connect() {
            HttpWebRequest request=(HttpWebRequest)WebRequest.Create(file.url);
            request.Proxy=null; request.AllowAutoRedirect=false; request.Timeout=30000; request.ReadWriteTimeout=30000;
            request.KeepAlive=true; request.AutomaticDecompression=DecompressionMethods.None;
            request.AddRange(position, file.size-1);
            lock (connectionLock) {
                if (owner.Cancelled) throw new OperationCanceledException();
                activeRequest=request;
            }
            response=(HttpWebResponse)request.GetResponse();
            if (owner.Cancelled) { request.Abort(); CloseConnection(); throw new OperationCanceledException(); }
            if (response.StatusCode != HttpStatusCode.PartialContent) { CloseConnection(); throw new IOException("Source must honor HTTP Range."); }
            string expected="bytes "+position.ToString(CultureInfo.InvariantCulture)+"-"+(file.size-1).ToString(CultureInfo.InvariantCulture)+"/"+file.size.ToString(CultureInfo.InvariantCulture);
            if (response.Headers["Content-Range"] != expected || response.ContentLength != file.size-position) { CloseConnection(); throw new IOException("Source size or range changed."); }
            input=response.GetResponseStream();
        }
        public int Read(IntPtr buffer, uint count, IntPtr actual) {
            if (actual != IntPtr.Zero) Marshal.WriteInt32(actual, 0);
            if (count == 0) return Native.S_OK;
            lock (readLock) {
                if (owner.Cancelled) return unchecked((int)0x800704C7);
                uint copied=0;
                try {
                    if (position >= file.size) return Native.S_FALSE;
                    owner.BeginRead();
                    if (input == null) Connect();
                    byte[] chunk=new byte[(int)Math.Min(1024*1024, Math.Min((long)count, file.size-position))];
                    while (copied < count && position < file.size) {
                        if (owner.Cancelled) return unchecked((int)0x800704C7);
                        int requested=(int)Math.Min(chunk.Length, Math.Min(count-copied, file.size-position));
                        int read=input.Read(chunk, 0, requested);
                        if (read == 0) throw new EndOfStreamException();
                        Marshal.Copy(chunk, 0, new IntPtr(buffer.ToInt64()+copied), read);
                        if (position <= contiguous) contiguous=Math.Max(contiguous, position+read);
                        position+=read; copied+=(uint)read; owner.Report(index, contiguous, read);
                    }
                    if (actual != IntPtr.Zero) Marshal.WriteInt32(actual, unchecked((int)copied));
                    if (position == file.size) CloseConnection();
                    return copied == count ? Native.S_OK : Native.S_FALSE;
                } catch (Exception error) {
                    if (actual != IntPtr.Zero) Marshal.WriteInt32(actual, unchecked((int)copied));
                    CloseConnection();
                    if (owner.Cancelled) return unchecked((int)0x800704C7);
                    owner.Failure(error); return Native.STG_E_READFAULT;
                }
            }
        }
        public int Write(IntPtr buffer, uint count, IntPtr actual) { if (actual != IntPtr.Zero) Marshal.WriteInt32(actual, 0); return Native.E_ACCESSDENIED; }
        public int Seek(long offset, uint origin, IntPtr result) {
            lock (readLock) {
                long next;
                try { next=checked((origin == 0 ? 0 : origin == 1 ? position : origin == 2 ? file.size : long.MinValue)+offset); } catch { return Native.STG_E_INVALIDFUNCTION; }
                if (origin > 2 || next < 0) return Native.STG_E_INVALIDFUNCTION;
                if (next != position) { CloseConnection(); position=next; }
                if (result != IntPtr.Zero) Marshal.WriteInt64(result, position);
                return Native.S_OK;
            }
        }
        public int SetSize(ulong size) { return Native.E_ACCESSDENIED; }
        public int CopyTo(IntPtr destination, ulong count, IntPtr read, IntPtr written) {
            ulong totalRead=0, totalWritten=0; IntPtr buffer=Marshal.AllocHGlobal(1024*1024), actual=Marshal.AllocHGlobal(4);
            try {
                IStream target=(IStream)Marshal.GetObjectForIUnknown(destination);
                while (totalRead < count) {
                    uint requested=(uint)Math.Min((ulong)(1024*1024), count-totalRead);
                    int hr=Read(buffer, requested, actual); int obtained=Marshal.ReadInt32(actual);
                    totalRead+=(uint)obtained;
                    if (obtained > 0) {
                        byte[] bytes=new byte[obtained]; Marshal.Copy(buffer, bytes, 0, obtained);
                        Marshal.WriteInt32(actual, 0); target.Write(bytes, obtained, actual); int saved=Marshal.ReadInt32(actual);
                        totalWritten+=(uint)saved;
                        if (saved != obtained) return unchecked((int)0x8003001D);
                    }
                    if (hr != Native.S_OK) return hr;
                }
                return Native.S_OK;
            } catch { return Native.STG_E_READFAULT; }
            finally {
                if (read != IntPtr.Zero) Marshal.WriteInt64(read, unchecked((long)totalRead));
                if (written != IntPtr.Zero) Marshal.WriteInt64(written, unchecked((long)totalWritten));
                Marshal.FreeHGlobal(buffer); Marshal.FreeHGlobal(actual);
            }
        }
        public int Commit(uint flags) { return Native.S_OK; }
        public int Revert() { return Native.STG_E_INVALIDFUNCTION; }
        public int LockRegion(ulong offset, ulong count, uint type) { return Native.STG_E_INVALIDFUNCTION; }
        public int UnlockRegion(ulong offset, ulong count, uint type) { return Native.STG_E_INVALIDFUNCTION; }
        public int Stat(out STATSTG stat, uint flags) { stat=new STATSTG { type=2, cbSize=file.size, grfMode=0, pwcsName=(flags & 1) != 0 ? null : file.name }; return Native.S_OK; }
        public int Clone(out INativeStream clone) { RemoteStream stream=new RemoteStream(owner, index); stream.position=position; owner.Register(stream); clone=stream; return Native.S_OK; }
    }

    internal static class Program {
        internal static readonly ConcurrentDictionary<string, VirtualFiles> Active=new ConcurrentDictionary<string, VirtualFiles>();
        private static readonly object OutputLock=new object(); private static readonly JavaScriptSerializer Json=new JavaScriptSerializer();
        internal static uint StaThread;
        internal static uint SourceProcess;
        internal static volatile VirtualFiles OleDrag;
        internal static Control Dispatcher;
        internal static bool PostToSta(Action action) {
            Control target=Dispatcher;
            try {
                if (target == null || target.IsDisposed || !target.IsHandleCreated) return false;
                target.BeginInvoke(action); return true;
            } catch (InvalidOperationException) { return false; }
        }
        private static bool dragging;
        internal static void Emit(object message) { lock (OutputLock) { Console.WriteLine(Json.Serialize(message)); Console.Out.Flush(); } }
        private static VirtualFiles Accept(Command command) {
            if (string.IsNullOrEmpty(command.id) || command.id.Length > 128 || command.files == null || command.files.Length == 0 || command.files.Length > 100) throw new ArgumentException();
            foreach (RemoteFile file in command.files) VirtualFiles.Validate(file);
            long total=0; foreach (RemoteFile file in command.files) total=checked(total+file.size);
            VirtualFiles files=new VirtualFiles(command.id, command.files);
            if (!string.IsNullOrEmpty(command.sourceWindow)) {
                if (SourceProcess == 0 || command.sourcePid != SourceProcess) throw new ArgumentException("The drag source process is unavailable.");
                files.SourceWindow=DragHost.Validate(command.sourceWindow, command.sourcePid);
            }
            if (!Active.TryAdd(command.id, files)) throw new ArgumentException();
            Emit(new { @event="accepted", id=command.id, total=total, buttonHeld=Native.LeftButtonHeld, sourceProvided=files.SourceWindow != IntPtr.Zero });
            return files;
        }
        private static void Begin(VirtualFiles files) {
            bool entered=false, oleEntered=false, buttonSynced=false, previousThreadButton=false; DragHost host=null; IDragSourceHelper visual=null;
            try {
                if (files.Finished) return;
                if (dragging) { files.Finish("error", "Finish the current drag before starting another Explorer copy."); return; }
                if (!Native.LeftButtonHeld) { files.Finish("error", "Keep the left mouse button held while dragging into Explorer. Try dragging again."); return; }
                // Handoff pumps the STA while awaiting the trusted parent's ACK.
                // A second Begin dispatched in that nested pump must not create
                // another host or reset this gesture's busy flag.
                dragging=true; entered=true;
                files.GestureActive=true;
                if (files.SourceWindow != IntPtr.Zero) {
                    bool foreground=Native.GetForegroundWindow() == files.SourceWindow;
                    if (!foreground) {
                        Emit(new { @event="handoff", id=files.Id, sourceForeground=false, buttonHeld=Native.LeftButtonHeld, attempted=false, succeeded=false });
                        files.Finish("error", "EyeCopy lost focus before the drag started. Return to EyeCopy and drag again while holding the left mouse button."); return;
                    }
                    host=new DragHost(files.SourceWindow);
                    bool handedOff=host.Handoff(files);
                    Emit(new { @event="handoff", id=files.Id, sourceForeground=foreground, buttonHeld=Native.LeftButtonHeld, threadButtonHeld=Native.ThreadLeftButtonHeld, attempted=true, succeeded=handedOff, helperForeground=Native.HelperForeground, hostActive=Native.GetActiveWindow() == host.Handle, hostFocused=Native.GetFocus() == host.Handle });
                    if (!handedOff) { files.Finish("error", "Windows could not start the Explorer drag. Return to EyeCopy and drag again while holding the left mouse button."); return; }
                }
                if (files.Finished) return;
                if (!Native.LeftButtonHeld) { files.Finish("error", "The mouse button was released before the drag started. Try dragging again and keep it held until you reach the Explorer folder."); return; }
                previousThreadButton=Native.ThreadLeftButtonHeld;
                if (host != null && Native.LeftButtonHeld && !previousThreadButton) {
                    // The initial mouse-down was delivered to Electron, not to
                    // this STA. Synchronize a proven thread/hardware mismatch
                    // only after the trusted gesture's foreground handoff.
                    Native.SetThreadLeftButton(true); buttonSynced=true; files.ThreadButtonAdjusted=true;
                }
                if (!Native.LeftButtonHeld) { files.Finish("error", "The mouse button was released before the drag started. Try dragging again and keep it held until you reach the Explorer folder."); return; }
                visual=DragVisual.Create(files);
                Emit(new { @event="drag_image_created", id=files.Id, succeeded=visual != null });
                if (!Native.LeftButtonHeld || files.Finished) { if (!files.Finished) files.Finish("error", "The mouse button was released before the drag started. Try dragging again and keep it held until you reach the Explorer folder."); return; }
                Emit(new { @event="begin", id=files.Id, buttonHeld=Native.LeftButtonHeld, threadButtonHeldBefore=previousThreadButton, threadButtonHeld=Native.ThreadLeftButtonHeld, threadButtonSynced=buttonSynced, helperForeground=Native.HelperForeground, ownCapture=Native.GetCapture() != IntPtr.Zero });
                oleEntered=true;
                OleDrag=files;
                uint effect; int result=Native.DoDragDrop(files, new DropSource(files), 1, out effect);
                files.InDragLoop=false;
                bool accepted=Native.CopyDrop(result, effect);
                Emit(new { @event="drag_end", id=files.Id, dropped=accepted, hresult=result, effect=effect, message=accepted ? null : result == Native.DRAGDROP_S_CANCEL ? "The drag was cancelled before a folder accepted the files." : "The mouse was released over a location that did not accept these files. Drop onto the file area or a folder inside Explorer." });
                if (!accepted) files.Finish("cancelled", null);
                else if (!files.AsyncStarted) files.FinishRead();
            } catch (Exception error) {
                Emit(new { @event="drag_exception", id=files.Id, hresult=error.HResult, stage=oleEntered ? "ole" : "handoff" });
                files.Finish("error", "The selected file could not be dragged. Check its filename, connection, and try again.");
            }
            finally {
                if (Object.ReferenceEquals(OleDrag, files)) OleDrag=null;
                if (visual != null) Marshal.ReleaseComObject(visual);
                if (files.ThreadButtonAdjusted) {
                    try { Native.SetThreadLeftButton(Native.LeftButtonHeld); }
                    catch { Emit(new { @event="drag_exception", id=files.Id, stage="restore_thread_button" }); }
                }
                if (host != null) { host.EndGesture(); host.Dispose(); }
                files.GestureActive=false; files.ReleasePrivateWhenReady();
                if (entered) dragging=false;
            }
        }
        [STAThread] private static int Main(string[] args) {
            Console.OutputEncoding=new UTF8Encoding(false);
            int hr=Native.OleInitialize(IntPtr.Zero); if (hr < 0) return 2;
            StaThread=Native.GetCurrentThreadId();
            try {
                if (args.Length != 0 && args[0] == "--self-test") return SelfTest.Run(args);
                if (args.Length == 3 && args[0] == "--shell-test") return SelfTest.Shell(args[1], args[2]);
                if (args.Length == 3 && args[0] == "--cancel-test") return SelfTest.Cancel(args[1], args[2]);
                if (args.Length == 2 && args[0] == "--marshal-server") return SelfTest.MarshalServer(args[1]);
                if (args.Length == 3 && args[0] == "--marshal-client") return SelfTest.MarshalClient(args[1], args[2]);
                if (args.Length != 0 && (args.Length != 2 || args[0] != "--parent" || !uint.TryParse(args[1], out SourceProcess) || SourceProcess == 0)) return 2;
                ServicePointManager.DefaultConnectionLimit=32;
                using (Control dispatcher=new Control()) {
                    dispatcher.CreateControl(); IntPtr handle=dispatcher.Handle;
                    Dispatcher=dispatcher;
                    Thread reader=new Thread(delegate() {
                        string line;
                        while ((line=Console.ReadLine()) != null) {
                            Command command=null;
                            try {
                                if (line.Length > 1024*1024) continue;
                                command=new JavaScriptSerializer().Deserialize<Command>(line);
                                if (command.command == "cancel") { VirtualFiles files; if (command.id != null && Active.TryGetValue(command.id, out files)) files.Cancel(); continue; }
                                if (command.command == "foreground_ack") { VirtualFiles files; if (command.id != null && Active.TryGetValue(command.id, out files)) files.ForegroundAck=command.succeeded ? 1 : -1; continue; }
                                if (command.command == "quit") { foreach (VirtualFiles files in Active.Values) files.Cancel(); dispatcher.BeginInvoke(new Action(Application.ExitThread)); return; }
                                if (command.command == "begin") { VirtualFiles files=Accept(command); dispatcher.BeginInvoke(new Action(delegate { Begin(files); })); }
                            } catch { Emit(new { @event="error", id=command == null ? null : command.id, message="Invalid Explorer drop request." }); }
                        }
                        dispatcher.BeginInvoke(new Action(Application.ExitThread));
                    });
                    reader.IsBackground=true; reader.Start(); Emit(new { @event="ready", protocol=1 }); Application.Run();
                    Dispatcher=null;
                }
                return 0;
            } finally { Native.OleUninitialize(); }
        }
    }

    internal static class SelfTest {
        internal static int Cancel(string url, string phase) {
            // A private mock endpoint stalls headers or the body. No mouse,
            // clipboard, Explorer window, or connected iPhone is accessed.
            if (phase != "headers" && phase != "body") throw new ArgumentException();
            RemoteFile file=new RemoteFile { name="cancel-test.bin", size=1048576+17, url=url }; VirtualFiles.Validate(file);
            VirtualFiles data=new VirtualFiles("cancel-test", new[] { file });
            Program.Active.TryAdd(data.Id, data);
            RemoteStream stream=new RemoteStream(data, 0); data.Register(stream);
            IntPtr buffer=Marshal.AllocHGlobal(65536), actual=Marshal.AllocHGlobal(4);
            int result=Native.S_OK;
            Thread reader=new Thread(delegate() { result=stream.Read(buffer, 65536, actual); }); reader.IsBackground=true;
            bool stopped=false;
            try {
                reader.Start(); Stopwatch waiting=Stopwatch.StartNew();
                while (!(phase == "body" ? stream.HasInput : stream.HasRequest) && waiting.ElapsedMilliseconds < 5000) Thread.Sleep(5);
                if (!(phase == "body" ? stream.HasInput : stream.HasRequest)) throw new Exception("Mock stream did not reach the blocked phase.");
                Thread.Sleep(50);
                Stopwatch cancelling=Stopwatch.StartNew(); data.Cancel();
                long acknowledgement=cancelling.ElapsedMilliseconds;
                stopped=reader.Join(1500);
                if (!stopped || result != unchecked((int)0x800704C7) || !data.Finished || Program.Active.ContainsKey(data.Id)) throw new Exception("Cancellation did not interrupt the blocked stream.");
                if (acknowledgement > 750) throw new Exception("Cancellation waited for the HTTP read lock.");
                // A late Shell callback cannot turn a cancelled transfer into
                // completion or emit a second terminal event.
                data.EndOperation(0, IntPtr.Zero, 1); data.Cancel();
                Program.Emit(new { @event="cancel_test_passed", phase=phase, acknowledgementMs=acknowledgement, stoppedMs=cancelling.ElapsedMilliseconds, helperOnly=true });
                return 0;
            } finally {
                data.Cancel(); if (!stopped) stopped=reader.Join(1500);
                if (stopped) { Marshal.FreeHGlobal(buffer); Marshal.FreeHGlobal(actual); }
            }
        }
        internal static int Shell(string url, string directory) {
            // The real Windows folder drop target consumes our virtual file;
            // tests call the COM data-transfer API without moving a mouse,
            // opening Explorer, or reading/modifying the user's clipboard.
            RemoteFile file=new RemoteFile { name="range-test.bin", size=1048576+17, url=url }; VirtualFiles.Validate(file);
            VirtualFiles data=new VirtualFiles("shell-test", new[] { file }); data.InDragLoop=false; data.AsyncMode=true;
            Program.Active.TryAdd(data.Id, data);
            return ShellCopy(data, directory, file.size, delegate() { return !data.AsyncStarted || data.Finished; }, delegate() { return data.AsyncStarted; }, "shell_test_passed");
        }
        private static int ShellCopy(DataObject data, string directory, long size, Func<bool> finished, Func<bool> asynchronous, string outcome) {
            directory=Path.GetFullPath(directory); Directory.CreateDirectory(directory);
            string destination=Path.Combine(directory, "range-test.bin");
            if (File.Exists(destination)) throw new IOException("Shell test destination must be fresh.");
            Guid shellId=new Guid("43826D1E-E718-42EE-BC55-A1E261C37BFE"), handler=new Guid("3981E225-F559-11D3-8E3A-00C04F6837D5"), targetId=new Guid("00000122-0000-0000-C000-000000000046");
            IShellItem item; Marshal.ThrowExceptionForHR(Native.SHCreateItemFromParsingName(directory, IntPtr.Zero, ref shellId, out item));
            IntPtr pointer; Marshal.ThrowExceptionForHR(item.BindToHandler(IntPtr.Zero, ref handler, ref targetId, out pointer));
            IDropTarget target=(IDropTarget)Marshal.GetObjectForIUnknown(pointer); Marshal.Release(pointer);
            try {
                uint effect=1; Marshal.ThrowExceptionForHR(target.DragEnter(data, 1, new PointL(), ref effect));
                if (!Native.CopyEffect(effect)) throw new Exception("Windows folder rejected the virtual-file format.");
                effect=1; Marshal.ThrowExceptionForHR(target.Drop(data, 0, new PointL(), ref effect));
                if (!Native.CopyEffect(effect)) throw new Exception("Windows folder rejected the virtual-file drop.");
                Stopwatch wait=Stopwatch.StartNew();
                while ((!File.Exists(destination) || new FileInfo(destination).Length != size || !finished()) && wait.ElapsedMilliseconds < 20000) { Application.DoEvents(); Thread.Sleep(20); }
                byte[] saved=File.ReadAllBytes(destination);
                if (saved.Length != size) throw new Exception("Windows folder wrote an incorrect file length.");
                for (int n=0; n<saved.Length; n++) if (saved[n] != (byte)(n%251)) throw new Exception("Windows folder wrote incorrect file content.");
                if (!finished()) throw new Exception("Asynchronous Shell completion callback did not finish.");
                Program.Emit(new { @event=outcome, bytes=saved.Length, realWindowsFolderTarget=true, streamedWithoutTempCopy=true, asyncShellCallback=asynchronous() });
                return 0;
            } finally { Marshal.ReleaseComObject(target); Marshal.ReleaseComObject(item); }
        }
        internal static int MarshalServer(string url) {
            RemoteFile file=new RemoteFile { name="range-test.bin", size=1048576+17, url=url }; VirtualFiles.Validate(file);
            VirtualFiles data=new VirtualFiles("cross-process-test", new[] { file });
            data.GestureActive=true;
            IDragSourceHelper visual=DragVisual.Create(data);
            if (visual == null || data.PrivateFormatCount == 0) throw new Exception("Cross-process source drag image did not initialize.");
            data.InDragLoop=false;
            Program.Active.TryAdd(data.Id, data);
            IStream packet; Marshal.ThrowExceptionForHR(Native.CreateStreamOnHGlobal(IntPtr.Zero, true, out packet));
            Guid dataId=new Guid("0000010E-0000-0000-C000-000000000046"); bool marshalled=false;
            try {
                // TABLESTRONG keeps the source alive until the receiver finishes;
                // its apartment stays in a real message loop throughout copying.
                Marshal.ThrowExceptionForHR(Native.CoMarshalInterface(packet, ref dataId, data, 0, IntPtr.Zero, 1)); marshalled=true;
                STATSTG stat; packet.Stat(out stat, 1);
                if (stat.cbSize <= 0 || stat.cbSize > 1048576) throw new Exception("Invalid marshal packet size.");
                byte[] bytes=new byte[(int)stat.cbSize]; packet.Seek(0, 0, IntPtr.Zero); packet.Read(bytes, bytes.Length, IntPtr.Zero);
                using (Control dispatcher=new Control()) {
                    IntPtr handle=dispatcher.Handle;
                    Thread reader=new Thread(delegate() { Console.ReadLine(); dispatcher.BeginInvoke(new Action(Application.ExitThread)); });
                    reader.IsBackground=true; reader.Start();
                    Program.Emit(new { @event="marshal_ready", packet=Convert.ToBase64String(bytes) }); Application.Run();
                }
                if (!data.Finished) throw new Exception("Cross-process Shell operation did not finish.");
                return 0;
            } finally {
                Marshal.ReleaseComObject(visual); data.GestureActive=false; data.ReleasePrivateWhenReady();
                if (marshalled) { packet.Seek(0, 0, IntPtr.Zero); Native.CoReleaseMarshalData(packet); }
                Marshal.ReleaseComObject(packet);
            }
        }
        internal static int MarshalClient(string value, string directory) {
            byte[] bytes=Convert.FromBase64String(value);
            if (bytes.Length > 1048576) throw new ArgumentException();
            IStream packet; Marshal.ThrowExceptionForHR(Native.CreateStreamOnHGlobal(IntPtr.Zero, true, out packet));
            try {
                packet.Write(bytes, bytes.Length, IntPtr.Zero); packet.Seek(0, 0, IntPtr.Zero);
                Guid dataId=new Guid("0000010E-0000-0000-C000-000000000046"); IntPtr pointer;
                Marshal.ThrowExceptionForHR(Native.CoUnmarshalInterface(packet, ref dataId, out pointer));
                DataObject data=(DataObject)Marshal.GetObjectForIUnknown(pointer); Marshal.Release(pointer);
                try {
                    FORMATETC image=new FORMATETC { cfFormat=Native.Format("DragImageBits"), dwAspect=DVASPECT.DVASPECT_CONTENT, lindex=-1, tymed=TYMED.TYMED_HGLOBAL };
                    STGMEDIUM medium; data.GetData(ref image, out medium);
                    try { if (Native.GlobalSize(medium.unionmember).ToUInt64() == 0) throw new Exception("Cross-process private drag image is empty."); } finally { Native.ReleaseStgMedium(ref medium); }
                    IDataObjectAsyncCapability async=(IDataObjectAsyncCapability)data;
                    return ShellCopy(data, directory, 1048576+17, delegate() { bool active; Marshal.ThrowExceptionForHR(async.InOperation(out active)); return !active; }, delegate() { bool mode; Marshal.ThrowExceptionForHR(async.GetAsyncMode(out mode)); return mode; }, "cross_process_test_passed");
                } finally { Marshal.ReleaseComObject(data); }
            } finally { Marshal.ReleaseComObject(packet); }
        }
        internal static int Run(string[] args) {
            // Never calls DoDragDrop, writes clipboard data, or touches a phone.
            using (Control window=new Control()) {
                IntPtr own=window.Handle; uint process=(uint)Process.GetCurrentProcess().Id;
                if (DragHost.Validate(own.ToInt64().ToString(CultureInfo.InvariantCulture), process) != own) throw new Exception("Own source window was rejected.");
                bool denied=false; try { DragHost.Validate(own.ToInt64().ToString(CultureInfo.InvariantCulture), process+1); } catch (ArgumentException) { denied=true; }
                if (!denied) throw new Exception("A foreign process window was accepted.");
                denied=false; try { DragHost.Validate("0", process); } catch (ArgumentException) { denied=true; }
                if (!denied) throw new Exception("An invalid source window was accepted.");
            }
            byte[] previous=new byte[256], changed=new byte[256];
            if (!Native.GetKeyboardState(previous)) throw new Exception("Cannot read helper thread key table.");
            bool held=(previous[1] & 0x80) != 0;
            try {
                Native.SetThreadLeftButton(!held);
                if (!Native.GetKeyboardState(changed) || ((changed[1] & 0x80) != 0) == held || (changed[1] & 0x7f) != (previous[1] & 0x7f)) throw new Exception("Helper thread button state did not change safely.");
                for (int key=0; key<256; key++) if (key != 1 && changed[key] != previous[key]) throw new Exception("Another helper-thread key state changed.");
            } finally { Native.SetThreadLeftButton(held); }
            RemoteFile file=new RemoteFile { name="large-test.mov", size=5L*1024*1024*1024+123, url="http://127.0.0.1:12345/abcdefghijklmnopqrstuvwxyz123456/original", modified="2026-10-09T12:00:00Z" };
            VirtualFiles.Validate(file); VirtualFiles data=new VirtualFiles("test", new[] { file }); byte[] descriptor=data.Descriptors();
            if (!Native.CopyDrop(Native.DRAGDROP_S_DROP, 1) || !Native.CopyDrop(Native.DRAGDROP_S_DROP, 0x80000001) || Native.CopyDrop(Native.DRAGDROP_S_DROP, 0) || Native.CopyDrop(Native.DRAGDROP_S_DROP, 2) || Native.CopyDrop(Native.DRAGDROP_S_DROP, 4) || Native.CopyDrop(Native.DRAGDROP_S_CANCEL, 1)) throw new Exception("Copy drop/scroll effect handling failed.");
            if (Native.ContinueDrag(false, false, 0, true, true) != Native.S_OK || Native.ContinueDrag(false, false, 1, false, true) != Native.DRAGDROP_S_DROP || Native.ContinueDrag(false, false, 1, true, true) != Native.S_OK || Native.ContinueDrag(false, false, 0, false, true) != Native.DRAGDROP_S_DROP || Native.ContinueDrag(true, false, 0, true, true) != Native.DRAGDROP_S_CANCEL || Native.ContinueDrag(false, true, 1, true, true) != Native.DRAGDROP_S_CANCEL || Native.ContinueDrag(false, false, 0, true, false) != Native.DRAGDROP_S_DROP) throw new Exception("Physical/queued button continuation handling failed.");
            PrivateFormats(data);
            PrivateLifetime(file);
            if (descriptor.Length != 596 || BitConverter.ToInt32(descriptor, 0) != 1 || BitConverter.ToUInt32(descriptor, 68) != 1 || BitConverter.ToUInt32(descriptor, 72) != 1073741947) throw new Exception("64-bit descriptor mismatch.");
            if (Encoding.Unicode.GetString(descriptor, 76, file.name.Length*2) != file.name) throw new Exception("Filename layout mismatch.");
            FORMATETC format=new FORMATETC { cfFormat=VirtualFiles.Contents, dwAspect=DVASPECT.DVASPECT_CONTENT, lindex=0, tymed=TYMED.TYMED_ISTREAM };
            if (data.QueryGetData(ref format) != Native.S_OK) throw new Exception("Stream format not recognized.");
            STGMEDIUM medium; data.GetData(ref format, out medium);
            try {
                INativeStream stream=(INativeStream)Marshal.GetObjectForIUnknown(medium.unionmember); STATSTG stat; stream.Stat(out stat, 1);
                if (stat.cbSize != file.size) throw new Exception("COM stream size mismatch.");
                IntPtr position=Marshal.AllocHGlobal(8); try { stream.Seek(file.size-9, 0, position); if (Marshal.ReadInt64(position) != file.size-9) throw new Exception("64-bit seek mismatch."); } finally { Marshal.FreeHGlobal(position); }
            } finally { Native.ReleaseStgMedium(ref medium); }
            bool rejected=false; try { VirtualFiles.Validate(new RemoteFile { name="..\\bad.mov", size=1, url=file.url }); } catch { rejected=true; } if (!rejected) throw new Exception("Traversal accepted.");
            rejected=false; try { VirtualFiles.Validate(new RemoteFile { name="bad.mov", size=1, url="http://example.com/abcdefghijklmnopqrstuvwxyz123456/original" }); } catch { rejected=true; } if (!rejected) throw new Exception("Remote URL accepted.");
            if (args.Length > 1) {
                RemoteFile actual=new RemoteFile { name="range-test.bin", size=1048576+17, url=args[1] }; VirtualFiles.Validate(actual); VirtualFiles owner=new VirtualFiles("range-test", new[] { actual }); RemoteStream stream=new RemoteStream(owner, 0);
                IntPtr buffer=Marshal.AllocHGlobal(131072), count=Marshal.AllocHGlobal(4), position=Marshal.AllocHGlobal(8);
                try {
                    long total=0; int result;
                    do {
                        result=stream.Read(buffer, 131072, count); int got=Marshal.ReadInt32(count); byte[] bytes=new byte[got]; Marshal.Copy(buffer, bytes, 0, got);
                        for (int n=0; n<got; n++) if (bytes[n] != (byte)((total+n)%251)) throw new Exception("Stream content mismatch."); total+=got;
                    } while (result == Native.S_OK);
                    if (result != Native.S_FALSE || total != actual.size) throw new Exception("Stream EOF mismatch.");
                    if (stream.Seek(999999, 0, position) != Native.S_OK || stream.Read(buffer, 99, count) != Native.S_OK || Marshal.ReadInt32(count) != 99 || Marshal.ReadByte(buffer) != (byte)(999999%251)) throw new Exception("Range seek mismatch.");
                } finally { Marshal.FreeHGlobal(buffer); Marshal.FreeHGlobal(count); Marshal.FreeHGlobal(position); stream.CloseConnection(); }
            }
            Program.Emit(new { @event="self_test_passed", descriptor64Bit=true, comStream=true, secureLoopback=true, sourceWindowOwnership=true, threadButtonStatePreservation=true, physicalButtonContinuation=true, privateFormatOwnership=true, privateFormatLifetime=true, shellDragImage=true, copyWithScroll=true, rangeRead=args.Length > 1 }); return 0;
        }
        private static void PrivateLifetime(RemoteFile file) {
            FORMATETC format=new FORMATETC { cfFormat=Native.Format("EyeCopy lifetime fixture"), dwAspect=DVASPECT.DVASPECT_CONTENT, lindex=-1, tymed=TYMED.TYMED_HGLOBAL };
            VirtualFiles active=new VirtualFiles("private-active-lifetime", new[] { file });
            active.GestureActive=true;
            STGMEDIUM input=Native.HGlobal(new byte[] {1,2,3}); active.SetData(ref format, ref input, true);
            Thread cancel=new Thread(active.Cancel); cancel.Start(); cancel.Join();
            if (!active.Finished || active.PrivateFormatCount != 1) throw new Exception("Cancellation released private data during an active gesture.");
            STGMEDIUM late=Native.HGlobal(new byte[] {9}); active.SetData(ref format, ref late, true);
            if (active.PrivateFormatCount != 1) throw new Exception("A late Shell SetData recreated terminal private state.");
            active.GestureActive=false; active.ReleasePrivateWhenReady();
            if (active.PrivateFormatCount != 0) throw new Exception("Ended gesture retained terminal private data.");
            // An asynchronous copy ends on another thread after OLE returns.
            // Native Shell ownership must be released back on the creating STA.
            using (Control dispatcher=new Control()) {
                IntPtr handle=dispatcher.Handle; Control previous=Program.Dispatcher; Program.Dispatcher=dispatcher;
                try {
                    VirtualFiles asynchronous=new VirtualFiles("private-async-lifetime", new[] { file });
                    input=Native.HGlobal(new byte[] {4,5,6}); asynchronous.SetData(ref format, ref input, true);
                    cancel=new Thread(asynchronous.Cancel); cancel.Start(); cancel.Join();
                    if (asynchronous.PrivateFormatCount != 1) throw new Exception("A non-STA terminal call released native private ownership.");
                    Stopwatch waiting=Stopwatch.StartNew();
                    while (asynchronous.PrivateFormatCount != 0 && waiting.ElapsedMilliseconds < 1000) Application.DoEvents();
                    if (asynchronous.PrivateFormatCount != 0) throw new Exception("The creating STA did not release asynchronous private data.");
                } finally { Program.Dispatcher=previous; }
            }
        }
        private static void PrivateFormats(VirtualFiles data) {
            FORMATETC format=new FORMATETC { cfFormat=Native.Format("EyeCopy native fixture"), dwAspect=DVASPECT.DVASPECT_CONTENT, lindex=-1, tymed=TYMED.TYMED_HGLOBAL };
            STGMEDIUM input=Native.HGlobal(new byte[] {1,2,3,4,5}), copy=new STGMEDIUM();
            try {
                data.SetData(ref format, ref input, false);
                if (Native.GlobalSize(input.unionmember).ToUInt64() < 5) throw new Exception("SetData(false) released the caller medium.");
                data.GetData(ref format, out copy); IntPtr pointer=Native.GlobalLock(copy.unionmember);
                if (pointer == IntPtr.Zero) throw new Exception("Private format medium cannot be locked.");
                try { if (Marshal.ReadByte(pointer) != 1) throw new Exception("Private format copy mismatch."); Marshal.WriteByte(pointer, 99); } finally { Native.GlobalUnlock(copy.unionmember); }
            } finally { if (copy.unionmember != IntPtr.Zero) Native.ReleaseStgMedium(ref copy); Native.ReleaseStgMedium(ref input); }
            data.GetData(ref format, out copy);
            try {
                IntPtr pointer=Native.GlobalLock(copy.unionmember); if (pointer == IntPtr.Zero) throw new Exception();
                try { if (Marshal.ReadByte(pointer) != 1) throw new Exception("Private GetData did not clone its medium."); } finally { Native.GlobalUnlock(copy.unionmember); }
            } finally { Native.ReleaseStgMedium(ref copy); }
            STGMEDIUM replacement=Native.HGlobal(new byte[] {9,8,7}); data.SetData(ref format, ref replacement, true);
            data.GetData(ref format, out copy);
            try {
                IntPtr pointer=Native.GlobalLock(copy.unionmember); if (pointer == IntPtr.Zero) throw new Exception();
                try { if (Marshal.ReadByte(pointer) != 9) throw new Exception("Private format replacement failed."); } finally { Native.GlobalUnlock(copy.unionmember); }
            } finally { Native.ReleaseStgMedium(ref copy); }
            STGMEDIUM large=Native.HGlobal(new byte[8*1024*1024+1]); bool rejected=false;
            try { data.SetData(ref format, ref large, false); } catch (Exception error) { if (error.HResult != Native.E_ACCESSDENIED) throw; rejected=true; } finally { Native.ReleaseStgMedium(ref large); }
            if (!rejected) throw new Exception("Oversized private drag metadata was accepted.");
            IDragSourceHelper visual=DragVisual.Create(data);
            if (visual == null || data.PrivateFormatCount < 2) throw new Exception("Windows Shell drag image did not initialize its private formats.");
            Marshal.ReleaseComObject(visual); data.ReleasePrivateData();
            if (data.PrivateFormatCount != 0 || data.QueryGetData(ref format) != Native.DV_E_FORMATETC) throw new Exception("Private drag data did not release.");
            FORMATETC hidden=new FORMATETC { cfFormat=15, dwAspect=DVASPECT.DVASPECT_CONTENT, lindex=-1, tymed=TYMED.TYMED_HGLOBAL };
            if (data.QueryGetData(ref hidden) == Native.S_OK) throw new Exception("The private Shell store leaked a filesystem drop format.");
        }
    }
}
