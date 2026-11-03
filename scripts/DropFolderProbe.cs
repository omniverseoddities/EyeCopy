// Read-only address and file-view probe for a fixed Explorer drop point.
// Never invokes controls, changes values, sends input, or navigates windows.
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using System.Text.RegularExpressions;
using System.Web.Script.Serialization;
using System.Windows.Automation;

internal static class DropFolderProbe {
    // Prefix declarations preserve the SDK vtable order. Reserved mutating
    // slots are never invoked; this probe calls only getters and QueryService.
    [ComImport, Guid("6d5140c1-7436-11ce-8034-00aa006009fa"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    private interface IServiceProviderRead { [PreserveSig] int QueryService(ref Guid service, ref Guid iid, out IntPtr result); }
    [ComImport, Guid("000214e2-0000-0000-c000-000000000046"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    private interface IShellBrowserRead {
        [PreserveSig] int GetWindow(out IntPtr window);
        [PreserveSig] int ReservedContextHelp([MarshalAs(UnmanagedType.Bool)] bool enter);
        [PreserveSig] int ReservedInsertMenus(IntPtr menu, IntPtr widths);
        [PreserveSig] int ReservedSetMenu(IntPtr menu, IntPtr descriptor, IntPtr window);
        [PreserveSig] int ReservedRemoveMenus(IntPtr menu);
        [PreserveSig] int ReservedStatusText(IntPtr text);
        [PreserveSig] int ReservedEnableModeless([MarshalAs(UnmanagedType.Bool)] bool enable);
        [PreserveSig] int ReservedTranslateAccelerator(IntPtr message, ushort id);
        [PreserveSig] int ReservedBrowseObject(IntPtr pidl, uint flags);
        [PreserveSig] int ReservedViewStateStream(uint mode, out IntPtr stream);
        [PreserveSig] int GetControlWindow(uint id, out IntPtr window);
        [PreserveSig] int ReservedSendControlMsg(uint id, uint message, IntPtr first, IntPtr second, out IntPtr result);
        [PreserveSig] int QueryActiveShellView(out IntPtr view);
    }
    [ComImport, Guid("000214e3-0000-0000-c000-000000000046"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    private interface IShellViewRead { [PreserveSig] int GetWindow(out IntPtr window); [PreserveSig] int ReservedContextHelp([MarshalAs(UnmanagedType.Bool)] bool enter); }
    [ComImport, Guid("cde725b0-ccc9-4519-917e-325d72fab4ce"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    private interface IFolderViewRead {
        [PreserveSig] int GetCurrentViewMode(out uint mode);
        [PreserveSig] int ReservedSetViewMode(uint mode);
        [PreserveSig] int GetFolder(ref Guid iid, out IntPtr folder);
    }
    [ComImport, Guid("1ac3d9f0-175c-11d1-95be-00609797ea4f"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    private interface IPersistFolderRead {
        [PreserveSig] int GetClassID(out Guid id);
        [PreserveSig] int ReservedInitialize(IntPtr pidl);
        [PreserveSig] int GetCurFolder(out IntPtr pidl);
    }
    [StructLayout(LayoutKind.Sequential)] private struct Point { internal int x, y; }
    [StructLayout(LayoutKind.Sequential)] private struct Bounds { internal int left, top, right, bottom; }
    private delegate bool EnumWindow(IntPtr window, IntPtr parameter);
    [DllImport("user32.dll")] private static extern bool EnumWindows(EnumWindow callback, IntPtr parameter);
    [DllImport("user32.dll")] private static extern bool IsWindowVisible(IntPtr window);
    [DllImport("user32.dll")] private static extern bool IsWindow(IntPtr window);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] private static extern int GetClassName(IntPtr window, StringBuilder text, int capacity);
    [DllImport("user32.dll")] private static extern IntPtr WindowFromPoint(Point point);
    [DllImport("user32.dll")] private static extern IntPtr GetAncestor(IntPtr window, uint mode);
    [DllImport("user32.dll")] private static extern uint GetWindowThreadProcessId(IntPtr window, out uint process);
    [DllImport("user32.dll")] private static extern bool GetWindowRect(IntPtr window, out Bounds bounds);
    [DllImport("user32.dll")] private static extern bool SetProcessDpiAwarenessContext(IntPtr context);
    [DllImport("user32.dll")] private static extern bool IsChild(IntPtr parent, IntPtr child);
    [DllImport("shell32.dll", CharSet = CharSet.Unicode)] [return: MarshalAs(UnmanagedType.Bool)] private static extern bool SHGetPathFromIDListEx(IntPtr pidl, StringBuilder path, uint count, uint options);
    private static readonly JavaScriptSerializer Json = new JavaScriptSerializer();
    private static string WindowClass(IntPtr window) { StringBuilder value = new StringBuilder(128); GetClassName(window, value, value.Capacity); return value.ToString(); }
    private static bool Explorer(IntPtr window, out uint process) {
        process = 0;
        if (!IsWindow(window) || !IsWindowVisible(window) || WindowClass(window) != "CabinetWClass" || GetWindowThreadProcessId(window, out process) == 0) return false;
        try {
            string executable = Process.GetProcessById((int)process).MainModule.FileName;
            return String.Equals(Path.GetFullPath(executable), Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.Windows), "explorer.exe"), StringComparison.OrdinalIgnoreCase);
        } catch { return false; }
    }
    private static string Short(string value) { return value == null ? "" : value.Length > 240 ? value.Substring(0, 240) : value; }
    private sealed class Node {
        internal AutomationElement element; internal int depth;
        internal Node(AutomationElement value, int level) { element = value; depth = level; }
    }
    private static List<object> Inventory(IntPtr window, uint process) {
        List<object> result = new List<object>(); Queue<Node> pending = new Queue<Node>();
        pending.Enqueue(new Node(AutomationElement.FromHandle(window), 0));
        while (pending.Count != 0 && result.Count < 160) {
            Node node = pending.Dequeue();
            if (node.element == null) continue;
            try {
                AutomationElement.AutomationElementInformation current = node.element.Current;
                if (current.ProcessId != (int)process) continue;
                System.Windows.Rect rect = current.BoundingRectangle;
                object valuePattern; string value = null;
                if (node.element.TryGetCurrentPattern(ValuePattern.Pattern, out valuePattern) && valuePattern is ValuePattern) value = ((ValuePattern)valuePattern).Current.Value;
                object textPattern; string documentText = null;
                if (current.Name == "Address Bar" && node.element.TryGetCurrentPattern(TextPattern.Pattern, out textPattern) && textPattern is TextPattern) documentText = ((TextPattern)textPattern).DocumentRange.GetText(32768);
                result.Add(new { depth = node.depth, name = Short(current.Name), help = Short(current.HelpText), className = current.ClassName, automationId = Short(current.AutomationId), controlType = current.ControlType == null ? "Unknown" : current.ControlType.ProgrammaticName, handle = current.NativeWindowHandle, offscreen = current.IsOffscreen, bounds = rect.IsEmpty ? null : new { x = rect.X, y = rect.Y, width = rect.Width, height = rect.Height }, value = Short(value), documentText = Short(documentText) });
                // Never inventory file names or thousands of rows in a folder.
                if (node.depth >= 9 || current.ClassName == "UIItemsView" || current.ClassName == "SysListView32" || current.ClassName == "SysTreeView32" || current.ControlType == ControlType.DataGrid || current.ControlType == ControlType.List) continue;
                AutomationElementCollection children = node.element.FindAll(TreeScope.Children, Condition.TrueCondition);
                for (int n = 0; n < children.Count && n < 80; n++) pending.Enqueue(new Node(children[n], node.depth + 1));
            } catch (ElementNotAvailableException) { }
            catch (InvalidOperationException) { }
        }
        return result;
    }
    private sealed class View {
        internal AutomationElement element; internal string name, className, value; internal System.Windows.Rect bounds; internal bool offscreen, valueReadOnly, focused;
    }
    private static List<View> Views(IntPtr window, uint process) {
        List<View> result = new List<View>(); Queue<Node> pending = new Queue<Node>(); pending.Enqueue(new Node(AutomationElement.FromHandle(window), 0));
        while (pending.Count != 0 && result.Count < 160) {
            Node node = pending.Dequeue(); if (node.element == null) continue;
            try {
                AutomationElement.AutomationElementInformation current = node.element.Current;
                if (current.ProcessId != (int)process) continue;
                View view = new View { element = node.element, name = current.Name ?? "", className = current.ClassName ?? "", bounds = current.BoundingRectangle, offscreen = current.IsOffscreen, focused = current.HasKeyboardFocus };
                object valuePattern;
                if (node.element.TryGetCurrentPattern(ValuePattern.Pattern, out valuePattern) && valuePattern is ValuePattern) { ValuePattern.ValuePatternInformation value = ((ValuePattern)valuePattern).Current; view.value = value.Value; view.valueReadOnly = value.IsReadOnly; }
                result.Add(view);
                if (node.depth >= 9 || view.className == "UIItemsView" || view.className == "SysListView32" || view.className == "SysTreeView32" || current.ControlType == ControlType.DataGrid || current.ControlType == ControlType.List) continue;
                AutomationElementCollection children = node.element.FindAll(TreeScope.Children, Condition.TrueCondition);
                for (int n = 0; n < children.Count && n < 80; n++) pending.Enqueue(new Node(children[n], node.depth + 1));
            } catch (ElementNotAvailableException) { } catch (InvalidOperationException) { }
        }
        return result;
    }
    private static bool LocalDirectory(string supplied, out string directory) {
        directory = null;
        if (String.IsNullOrEmpty(supplied) || supplied.Length > 32767 || !Regex.IsMatch(supplied, @"^[a-zA-Z]:\\") || supplied.IndexOf('\0') >= 0) return false;
        try {
            string path = Path.GetFullPath(supplied);
            DriveType type = new DriveInfo(Path.GetPathRoot(path)).DriveType;
            if ((type != DriveType.Fixed && type != DriveType.Removable) || !Directory.Exists(path) || (File.GetAttributes(path) & FileAttributes.ReparsePoint) != 0) return false;
            directory = path; return true;
        } catch { return false; }
    }
    private static bool Inside(System.Windows.Rect rect, int x, int y) { return !rect.IsEmpty && rect.Width > 0 && rect.Height > 0 && rect.Contains(new System.Windows.Point(x, y)); }
    private static bool Same(AutomationElement first, AutomationElement second) { return first != null && second != null && Automation.Compare(first, second); }
    private sealed class ShellViewRecord {
        public string rootWindow, viewWindow, directory, stage;
        public bool visible, filesystem, localDirectory;
        public int hresult;
        internal IntPtr nativeRoot, nativeView;
    }
    private static void Release(object value) { if (value != null && Marshal.IsComObject(value)) Marshal.ReleaseComObject(value); }
    private static List<ShellViewRecord> ShellViews(IntPtr targetRoot) {
        List<ShellViewRecord> result = new List<ShellViewRecord>(); object shell = null, windows = null;
        try {
            shell = Activator.CreateInstance(Type.GetTypeFromProgID("Shell.Application", true));
            windows = ((dynamic)shell).Windows(); int count = Math.Min((int)((dynamic)windows).Count, 64);
            for (int n = 0; n < count; n++) {
                object window = null, browser = null, view = null, folder = null; IntPtr pointer = IntPtr.Zero, pidl = IntPtr.Zero;
                ShellViewRecord record = new ShellViewRecord { stage = "browser entry" };
                try {
                    window = ((dynamic)windows).Item(n); if (window == null) continue;
                    record.nativeRoot = new IntPtr(Convert.ToInt64(((dynamic)window).HWND)); uint process;
                    if ((targetRoot != IntPtr.Zero && record.nativeRoot != targetRoot) || !Explorer(record.nativeRoot, out process)) continue;
                    record.rootWindow = record.nativeRoot.ToInt64().ToString(); result.Add(record);
                    record.stage = "top-level browser service";
                    Guid service = new Guid("4c96be40-915c-11cf-99d3-00aa004ae837"), browserId = new Guid("000214e2-0000-0000-c000-000000000046");
                    Marshal.ThrowExceptionForHR(((IServiceProviderRead)window).QueryService(ref service, ref browserId, out pointer));
                    if (pointer == IntPtr.Zero) throw new InvalidOperationException("Browser service unavailable.");
                    browser = Marshal.GetObjectForIUnknown(pointer); Marshal.Release(pointer); pointer = IntPtr.Zero;
                    record.stage = "active shell view";
                    Marshal.ThrowExceptionForHR(((IShellBrowserRead)browser).QueryActiveShellView(out pointer));
                    if (pointer == IntPtr.Zero) throw new InvalidOperationException("Active view unavailable.");
                    view = Marshal.GetObjectForIUnknown(pointer); Marshal.Release(pointer); pointer = IntPtr.Zero;
                    record.stage = "view window";
                    Marshal.ThrowExceptionForHR(((IShellViewRead)view).GetWindow(out record.nativeView));
                    record.viewWindow = record.nativeView.ToInt64().ToString(); record.visible = IsWindowVisible(record.nativeView);
                    if (!IsWindow(record.nativeView) || GetAncestor(record.nativeView, 2) != record.nativeRoot) throw new InvalidOperationException("Active view is outside its Explorer window.");
                    record.stage = "active folder object";
                    Guid folderId = new Guid("1ac3d9f0-175c-11d1-95be-00609797ea4f");
                    Marshal.ThrowExceptionForHR(((IFolderViewRead)view).GetFolder(ref folderId, out pointer));
                    if (pointer == IntPtr.Zero) throw new InvalidOperationException("Active folder unavailable.");
                    folder = Marshal.GetObjectForIUnknown(pointer); Marshal.Release(pointer); pointer = IntPtr.Zero;
                    record.stage = "active folder PIDL";
                    int status = ((IPersistFolderRead)folder).GetCurFolder(out pidl); Marshal.ThrowExceptionForHR(status);
                    if (status != 0 || pidl == IntPtr.Zero) throw new InvalidOperationException("Active folder is not initialized.");
                    StringBuilder path = new StringBuilder(32768); record.filesystem = SHGetPathFromIDListEx(pidl, path, 32768, 0);
                    string directory = null; record.localDirectory = record.filesystem && LocalDirectory(path.ToString(), out directory);
                    record.directory = record.localDirectory ? directory : null; record.stage = "read";
                } catch (Exception error) { record.hresult = error.HResult; }
                finally { if (pidl != IntPtr.Zero) Marshal.FreeCoTaskMem(pidl); if (pointer != IntPtr.Zero) Marshal.Release(pointer); Release(folder); Release(view); Release(browser); Release(window); }
            }
        } finally { Release(windows); Release(shell); }
        return result;
    }
    private static object Resolve(int x, int y) {
        Point point = new Point { x = x, y = y }; IntPtr hit = WindowFromPoint(point), root = GetAncestor(hit, 2); uint process;
        if (!Explorer(root, out process)) return new { verified = false, reason = "The fixed drop point is not in an ordinary Explorer window." };
        List<View> views = Views(root, process); View fileView = null;
        foreach (View view in views) if (!view.offscreen && (view.className == "UIItemsView" || view.className == "SysListView32") && Inside(view.bounds, x, y)) { if (fileView != null) return new { verified = false, reason = "More than one file view overlaps the fixed drop point." }; fileView = view; }
        if (fileView == null) return new { verified = false, reason = "Drop into the empty file area, away from the sidebar, search, toolbar, or preview pane." };
        AutomationElement underPoint = AutomationElement.FromPoint(new System.Windows.Point(x, y));
        if (!Same(underPoint, fileView.element)) return new { verified = false, reason = "The fixed drop point is over an item, header, scrollbar, or overlay. Use the empty file area." };
        string directory = null, shellView = null;
        IntPtr itemHandle = new IntPtr(fileView.element.Current.NativeWindowHandle);
        foreach (ShellViewRecord shell in ShellViews(root)) {
            Bounds nativeBounds;
            if (!shell.localDirectory || !shell.visible || shell.nativeView == root || !GetWindowRect(shell.nativeView, out nativeBounds)
                || x < nativeBounds.left || x >= nativeBounds.right || y < nativeBounds.top || y >= nativeBounds.bottom
                || itemHandle == IntPtr.Zero || (shell.nativeView != itemHandle && !IsChild(shell.nativeView, itemHandle))) continue;
            if (directory != null && !String.Equals(directory, shell.directory, StringComparison.OrdinalIgnoreCase)) return new { verified = false, reason = "Visible Shell views expose conflicting directories." };
            directory = shell.directory; shellView = shell.viewWindow;
        }
        if (directory == null) return new { verified = false, reason = "Explorer did not expose a verified local active-view path. Use Download to folder.", rootWindow = root.ToInt64().ToString(), processId = process, itemViewVerified = true };
        // Recheck fixed point and HWND ownership after provider calls.
        uint currentProcess; Bounds currentBounds;
        if (!Explorer(root, out currentProcess) || currentProcess != process || GetAncestor(WindowFromPoint(point), 2) != root || !GetWindowRect(root, out currentBounds) || !Same(AutomationElement.FromPoint(new System.Windows.Point(x, y)), fileView.element)) return new { verified = false, reason = "Explorer changed during the read-only target check." };
        bool stable = false;
        foreach (ShellViewRecord current in ShellViews(root)) if (current.localDirectory && current.visible && current.viewWindow == shellView && String.Equals(current.directory, directory, StringComparison.OrdinalIgnoreCase)) stable = true;
        if (!stable || GetAncestor(WindowFromPoint(point), 2) != root || !Same(AutomationElement.FromPoint(new System.Windows.Point(x, y)), fileView.element)) return new { verified = false, reason = "The active Explorer folder changed during target verification." };
        return new { verified = true, directory = directory, rootWindow = root.ToInt64().ToString(), viewWindow = shellView, processId = process, itemViewClass = fileView.className, addressSource = "COM active visible Shell view", coordinateSpace = "physical pixels", dropAccepted = false, readOnly = true, markerConsumed = false };
    }
    [STAThread] private static int Main(string[] args) {
        Console.OutputEncoding = new UTF8Encoding(false);
        try {
            if (!SetProcessDpiAwarenessContext(new IntPtr(-4))) throw new InvalidOperationException("Physical pixel awareness is unavailable.");
            if (args.Length == 1 && args[0] == "--inventory") {
                List<object> windows = new List<object>();
                EnumWindow callback = delegate(IntPtr window, IntPtr parameter) { uint process; if (Explorer(window, out process)) { Bounds bounds; GetWindowRect(window, out bounds); windows.Add(new { rootWindow = window.ToInt64().ToString(), processId = process, bounds = new { x = bounds.left, y = bounds.top, width = bounds.right - bounds.left, height = bounds.bottom - bounds.top }, elements = Inventory(window, process) }); } return windows.Count < 16; };
                EnumWindows(callback, IntPtr.Zero); GC.KeepAlive(callback);
                Console.WriteLine(Json.Serialize(new { readOnly = true, windows = windows, shellViews = ShellViews(IntPtr.Zero) })); return 0;
            }
            int x, y;
            if (args.Length != 3 || args[0] != "--point" || !int.TryParse(args[1], out x) || !int.TryParse(args[2], out y)) throw new ArgumentException("Use --inventory or --point x y.");
            Console.WriteLine(Json.Serialize(Resolve(x, y))); return 0;
        } catch (Exception error) { Console.WriteLine(Json.Serialize(new { verified = false, reason = "Explorer's read-only address could not be verified.", errorType = error.GetType().Name, diagnostic = args.Length == 1 && args[0] == "--inventory" ? error.StackTrace : null })); return 1; }
    }
}
