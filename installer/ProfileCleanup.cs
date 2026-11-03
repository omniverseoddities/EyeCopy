// SPDX-License-Identifier: GPL-3.0-or-later
// Removes only verified application files and registered, owned profile data.
using System;
using System.IO;
using System.Text;
using System.Linq;
using System.Collections;
using System.Collections.Generic;
using System.Diagnostics;
using System.Security.Cryptography;
using System.Web.Script.Serialization;
using Microsoft.Win32;
using System.Runtime.InteropServices;
using System.Reflection;

internal static class ProfileCleanup {
    const string AppId = "com.eyecopy.desktop";
    static readonly JavaScriptSerializer Json = new JavaScriptSerializer { MaxJsonLength = 64 * 1024 * 1024, RecursionLimit = 200 };
    static readonly StringComparer Names = StringComparer.OrdinalIgnoreCase;
    static readonly HashSet<string> ProfileFolders = new HashSet<string>(new [] {"cache", "desktop", "drag-tickets", "marker-sources", "native-drag", "marker-probe", "drop-folder-probe", "updates"}, Names);
    static readonly HashSet<string> ProfileFiles = new HashSet<string>(new [] {"state.json", "folder-rename.json", "desktop-settings.json", "desktop.log", "usb-diagnostics.json", "recycle-undo.json", "recycle-undo-journal.json"}, Names);
    static readonly List<string> Kept = new List<string>(), Errors = new List<string>();
    static string TestRoot, AppRoot, RegistryPath;
    static bool Execute;
    static int Deleted;
    [DllImport("shell32.dll")] static extern int SHGetKnownFolderPath(ref Guid id,uint flags,IntPtr token,out IntPtr path);
    static string DownloadsFolder() {
        var id=new Guid("374DE290-123F-4565-9164-39C4925E467B");IntPtr pointer;
        if(SHGetKnownFolderPath(ref id,0,IntPtr.Zero,out pointer)!=0)return Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile),"Downloads");
        try{return Marshal.PtrToStringUni(pointer);}finally{Marshal.FreeCoTaskMem(pointer);}
    }

    static string Canonical(string value) {
        if (String.IsNullOrWhiteSpace(value) || !System.Text.RegularExpressions.Regex.IsMatch(value,@"^[A-Za-z]:[\\/]") || value.IndexOf(':', 2) >= 0)
            throw new InvalidOperationException("Only ordinary absolute local paths are supported.");
        return Path.GetFullPath(value).TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar);
    }
    static bool Same(string a, string b) { return Names.Equals(a, b); }
    static bool Inside(string path, string root) { return Same(path, root) || path.StartsWith(root + Path.DirectorySeparatorChar, StringComparison.OrdinalIgnoreCase); }
    static void NoLinks(string path) {
        string cursor = path;
        while (!String.IsNullOrEmpty(cursor)) {
            if ((Directory.Exists(cursor) || File.Exists(cursor)) && (File.GetAttributes(cursor) & FileAttributes.ReparsePoint) != 0)
                throw new InvalidOperationException("Linked or redirected paths require manual review: " + path);
            var parent = Directory.GetParent(cursor); cursor = parent == null ? null : parent.FullName;
        }
    }
    static void SafeRoot(string path, bool application) {
        NoLinks(path);
        string drive = Path.GetPathRoot(path).TrimEnd('\\');
        if (Same(path, drive)) throw new InvalidOperationException("A drive root cannot be removed.");
        string home = Canonical(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile));
        var exact = new List<string> {home, Directory.GetParent(home).FullName,
            Canonical(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData)),
            Canonical(Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData)),
            Canonical(Path.Combine(home,"AppData")), Canonical(Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),"Programs")),
            Canonical(Environment.GetFolderPath(Environment.SpecialFolder.MyDocuments)), Canonical(Environment.GetFolderPath(Environment.SpecialFolder.MyVideos)),
            Canonical(Environment.GetFolderPath(Environment.SpecialFolder.MyPictures)), Canonical(Environment.GetFolderPath(Environment.SpecialFolder.DesktopDirectory)),Canonical(DownloadsFolder()),Canonical(Path.GetTempPath())};
        if (exact.Any(value => Same(path,value))) throw new InvalidOperationException("A shared user folder cannot be removed.");
        foreach (var folder in new [] {Environment.SpecialFolder.Windows, Environment.SpecialFolder.ProgramFiles, Environment.SpecialFolder.ProgramFilesX86, Environment.SpecialFolder.CommonApplicationData}) {
            string value = Environment.GetFolderPath(folder);
            if (!String.IsNullOrEmpty(value) && Inside(path,Canonical(value))) throw new InvalidOperationException("A protected system folder cannot be removed.");
        }
        if (TestRoot != null && !Inside(path, TestRoot)) throw new InvalidOperationException("A test operation escaped its isolated root.");
        if (!application && (Inside(path,AppRoot) || Inside(AppRoot,path))) throw new InvalidOperationException("Overlapping application/profile folders require manual review.");
    }
    static Dictionary<string,object> Read(string file) {
        if (new FileInfo(file).Length > 64 * 1024 * 1024) throw new InvalidOperationException("A metadata file is too large for safe cleanup.");
        return Json.Deserialize<Dictionary<string,object>>(File.ReadAllText(file,Encoding.UTF8));
    }
    static string Str(Dictionary<string,object> value, string name) { object item; return value.TryGetValue(name,out item) ? item as string : null; }
    static void Marker(string folder, string name, string kind) {
        string path = Path.Combine(folder,name); NoLinks(path); var marker = Read(path);
        object schema;
        if (Str(marker,"applicationId") != AppId || !marker.TryGetValue("schema",out schema) || Convert.ToInt32(schema) != 1 || (kind != null && Str(marker,"kind") != kind))
            throw new InvalidOperationException("The folder is not marked as owned by this application: " + folder);
    }
    static string Hash(string file) { using(var sha=SHA256.Create()) using(var stream=File.OpenRead(file)) return BitConverter.ToString(sha.ComputeHash(stream)).Replace("-", "").ToLowerInvariant(); }
    static string KeyFor(string value) {
        // Match the desktop's byte-stable path identity across Unicode runtimes.
        char[] characters=value.ToCharArray();
        for(int i=0;i<characters.Length;i++)if(characters[i]>='A' && characters[i]<='Z')characters[i]=(char)(characters[i]+('a'-'A'));
        using(var sha=SHA256.Create())return BitConverter.ToString(sha.ComputeHash(Encoding.UTF8.GetBytes(new String(characters)))).Replace("-", "").ToLowerInvariant();
    }
    static IEnumerable<object> ArrayOf(object value) { var list=value as IEnumerable; if(list != null && !(value is string) && !(value is IDictionary)) foreach(var entry in list) yield return entry; }
    static void ProtectPaths(object value, HashSet<string> paths, bool includeDirectories=true) {
        var map=value as Dictionary<string,object>;
        if(map != null) {
            foreach(var entry in map) {
                if((new [] {"path", "localPath", "finalPath", "partPath", "originalPath", "binPath"}.Contains(entry.Key) || includeDirectories && new [] {"defaultDownloadDir", "downloadDir", "downloadRootDir"}.Contains(entry.Key)) && entry.Value is string) {
                    try { var path=Canonical((string)entry.Value); paths.Add(path); } catch {}
                }
                if(entry.Value is Dictionary<string,object> || (entry.Value is IEnumerable && !(entry.Value is string))) ProtectPaths(entry.Value,paths,includeDirectories);
            }
        } else foreach(var entry in ArrayOf(value)) ProtectPaths(entry,paths,includeDirectories);
    }
    static void DeleteTree(string path, HashSet<string> protectedPaths) {
        NoLinks(path);
        if(protectedPaths.Any(p=>Inside(p,path) || Inside(path,p))) { Kept.Add("Downloaded or user-managed files were kept: " + path); return; }
        if(Directory.Exists(path)) {
            foreach(string child in Directory.GetFileSystemEntries(path)) DeleteTree(child, protectedPaths);
            if(Execute && !Directory.EnumerateFileSystemEntries(path).Any()) Directory.Delete(path);
        } else if(File.Exists(path)) { if(Execute) { File.SetAttributes(path,File.GetAttributes(path) & ~FileAttributes.ReadOnly); File.Delete(path); } Deleted++; }
    }
    static bool AtomicProfileFile(string name) {
        return System.Text.RegularExpressions.Regex.IsMatch(name,@"^(state\.json|folder-rename\.json|desktop-settings\.json)\.[0-9a-fA-F-]{32,36}\.tmp$|^recycle-undo-[0-9a-fA-F]{32}\.tmp$");
    }
    static void CheckTree(string path) { NoLinks(path); if(Directory.Exists(path)) foreach(string child in Directory.GetFileSystemEntries(path)) CheckTree(child); }
    static void Profile(string path, string registryName) {
        SafeRoot(path,false); if(!Directory.Exists(path)) { if(Execute && registryName!=null) RemoveRegistration(registryName,path); return; }
        Marker(path,".eyecopy-profile.json","profile");
        var protectedPaths = new HashSet<string>(Names); string state=Path.Combine(path,"state.json");
        if(File.Exists(state)) { NoLinks(state); ProtectPaths(Read(state),protectedPaths); }
        else if(Directory.GetFileSystemEntries(path).Any(p=>ProfileFolders.Contains(Path.GetFileName(p))))
            throw new InvalidOperationException("Profile history is missing, so downloaded-file protection requires manual review: " + path);
        var known = new List<string>();
        foreach(string child in Directory.GetFileSystemEntries(path)) {
            string name=Path.GetFileName(child);
            if(name==".eyecopy-profile.json") continue;
            if(ProfileFiles.Contains(name) || AtomicProfileFile(name) || ProfileFolders.Contains(name)) { CheckTree(child); known.Add(child); }
            else Kept.Add("Unrecognized profile item was kept: " + child);
        }
        // Keep tracking/guard files until all data directories have been handled.
        foreach(string child in known.OrderBy(p=>Directory.Exists(p)?0:1).ThenBy(p=>ProfileFiles.Contains(Path.GetFileName(p))?1:0)) DeleteTree(child,protectedPaths);
        if(Execute) {
            var left=Directory.GetFileSystemEntries(path).Where(p=>Path.GetFileName(p)!=".eyecopy-profile.json").ToArray();
            if(left.Length==0) { File.Delete(Path.Combine(path,".eyecopy-profile.json")); Directory.Delete(path); if(registryName!=null)RemoveRegistration(registryName,path); }
        }
    }
    static void RemoveRegistration(string name,string path) {
        using(var root=Registry.CurrentUser.OpenSubKey(RegistryPath,true)) {
            if(root==null)return;
            using(var key=root.OpenSubKey(name)) { if(key==null || !Same(Canonical(key.GetValue("Path") as string),path))return; }
            root.DeleteSubKeyTree(name,false);
        }
    }
    static Dictionary<string,string> Profiles() {
        var result=new Dictionary<string,string>(Names);
        using(var root=Registry.CurrentUser.OpenSubKey(RegistryPath)) if(root!=null) foreach(var name in root.GetSubKeyNames()) {
            if(!System.Text.RegularExpressions.Regex.IsMatch(name,@"^[0-9a-f]{64}$"))continue;
            using(var key=root.OpenSubKey(name)) { var value=key.GetValue("Path") as string; if(value!=null) {string path=Canonical(value);if(!Same(name,KeyFor(path)))throw new InvalidOperationException("A profile registration has an invalid identity.");result[path]=name;} }
        }
        if(TestRoot==null) {
            string standard=Canonical(Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),"EyeCopy"));
            if(File.Exists(Path.Combine(standard,".eyecopy-profile.json")) && !result.ContainsKey(standard))result[standard]=null;
        }
        return result;
    }
    static bool PublicApplication(string folder) { try { Marker(folder,".eyecopy-install.json","application"); return true; } catch { return false; } }
    static void RequireClosed() {
        foreach(var process in Process.GetProcessesByName("EyeCopy")) {
            try { string executable=process.MainModule.FileName; string folder=Canonical(Path.GetDirectoryName(executable));
                if(Same(folder,AppRoot) || (TestRoot==null && PublicApplication(folder))) throw new InvalidOperationException("Quit EyeCopy from its notification-area menu, then run Uninstall again.");
            } catch(System.ComponentModel.Win32Exception) { throw new InvalidOperationException("A running EyeCopy process could not be checked. Close it before uninstalling."); }
            catch(InvalidOperationException) { throw; }
            finally { process.Dispose(); }
        }
    }
    static Dictionary<string,object> ManifestFiles(string path) {
        NoLinks(path);var manifest=Read(path);object value;
        if(Str(manifest,"application")!="EyeCopy" || !manifest.TryGetValue("sourceFiles",out value) || !(value is Dictionary<string,object>))throw new InvalidOperationException("The application file inventory is invalid.");
        var files=(Dictionary<string,object>)value;
        foreach(var entry in files) {
            ManifestTarget(entry.Key);
            if(!(entry.Value is string) || !System.Text.RegularExpressions.Regex.IsMatch((string)entry.Value,@"^[0-9a-fA-F]{64}$"))throw new InvalidOperationException("The application inventory contains an invalid checksum.");
        }
        return files;
    }
    static string ManifestTarget(string relative) {
        relative=relative.Replace('/',Path.DirectorySeparatorChar);
        if(Path.IsPathRooted(relative) || relative.Split('\\').Any(p=>p==".." || p=="." || p.Length==0) || relative.Contains(':'))throw new InvalidOperationException("The application inventory contains an unsafe path.");
        string target=Canonical(Path.Combine(AppRoot,relative));
        if(!Inside(target,AppRoot) || Same(target,AppRoot))throw new InvalidOperationException("The application inventory escaped its folder.");
        NoLinks(target);return target;
    }
    static void ValidateInstall(string incoming) {
        if(incoming==null)throw new InvalidOperationException("Setup requires its incoming application inventory.");
        var next=ManifestFiles(incoming);Dictionary<string,object> previous=null;
        bool existing=Directory.Exists(AppRoot) && Directory.EnumerateFileSystemEntries(AppRoot).Any();
        if(existing) {Marker(AppRoot,".eyecopy-install.json","application");RequireClosed();previous=ManifestFiles(Path.Combine(AppRoot,".eyecopy-build.json"));}
        var protectedFiles=new HashSet<string>(Names);
        foreach(var profile in Profiles()) {
            if(!Directory.Exists(profile.Key))continue;
            SafeRoot(profile.Key,false);Marker(profile.Key,".eyecopy-profile.json","profile");
            string state=Path.Combine(profile.Key,"state.json");
            if(File.Exists(state)) {NoLinks(state);ProtectPaths(Read(state),protectedFiles,false);}
        }
        if(protectedFiles.Contains(Path.Combine(AppRoot,".eyecopy-build.json")))throw new InvalidOperationException("Setup would replace a tracked downloaded file. Choose another application folder.");
        foreach(var entry in next) {
            string target=ManifestTarget(entry.Key);
            if(Directory.Exists(target))throw new InvalidOperationException("Setup would replace an existing directory: "+target);
            var parent=Directory.GetParent(target);
            while(parent!=null && !Same(parent.FullName,AppRoot)) {if(File.Exists(parent.FullName))throw new InvalidOperationException("A file blocks an incoming application directory: "+parent.FullName);parent=parent.Parent;}
            if(!File.Exists(target))continue;
            object oldHash;
            if(protectedFiles.Contains(target))throw new InvalidOperationException("Setup would replace a tracked downloaded file. Choose another application folder: "+target);
            if(previous==null || !previous.TryGetValue(entry.Key,out oldHash) || !(oldHash is string) || !Names.Equals(Hash(target),(string)oldHash))
                throw new InvalidOperationException("Setup would replace an unrecognized or changed file. Move that file or choose another application folder: "+target);
        }
    }
    static void Application() {
        SafeRoot(AppRoot,true); Marker(AppRoot,".eyecopy-install.json","application");
        string manifestPath=Path.Combine(AppRoot,".eyecopy-build.json");NoLinks(manifestPath);var manifest=Read(manifestPath);
        if(Str(manifest,"application")!="EyeCopy") throw new InvalidOperationException("The application manifest is invalid.");
        object value; if(!manifest.TryGetValue("sourceFiles",out value)) throw new InvalidOperationException("The application manifest has no file inventory.");
        var files=value as Dictionary<string,object>;if(files==null)throw new InvalidOperationException("The application inventory is invalid.");
        var removable=new List<string>();
        foreach(var entry in files) {
            string relative=entry.Key.Replace('/',Path.DirectorySeparatorChar);
            if(Path.IsPathRooted(relative) || relative.Split('\\').Any(p=>p==".." || p==".") || relative.Contains(':'))throw new InvalidOperationException("The application inventory contains an unsafe path.");
            string target=Canonical(Path.Combine(AppRoot,relative));if(!Inside(target,AppRoot) || Same(target,AppRoot))throw new InvalidOperationException("The application inventory escaped its folder.");NoLinks(target);
            if(!File.Exists(target))continue;
            if(Same(target,Path.Combine(AppRoot,"UninstallEyeCopy.exe"))) { if(!(entry.Value is string) || Hash(target)!=(string)entry.Value)throw new InvalidOperationException("The uninstaller does not match its application inventory.");continue; }
            if(entry.Value is string && Hash(target)==(string)entry.Value)removable.Add(target);else Kept.Add("Changed application file was kept: " + target);
        }
        foreach(string file in removable) { if(Execute) {File.SetAttributes(file,File.GetAttributes(file)&~FileAttributes.ReadOnly);File.Delete(file);}Deleted++; }
        if(Execute) {
            File.Delete(manifestPath);
            EmptyDirectories(AppRoot);
        }
    }
    static void EmptyDirectories(string root) {
        foreach(string directory in Directory.GetDirectories(root)) {
            if((File.GetAttributes(directory)&FileAttributes.ReparsePoint)!=0) {Kept.Add("A linked directory was kept: "+directory);continue;}
            EmptyDirectories(directory);if(!Directory.EnumerateFileSystemEntries(directory).Any())Directory.Delete(directory);
        }
    }
    static void OwnedShortcut(string path) {
        if(!File.Exists(path))return;NoLinks(path);object shell=null,shortcut=null;
        try {
            shell=Activator.CreateInstance(Type.GetTypeFromProgID("WScript.Shell"));
            shortcut=shell.GetType().InvokeMember("CreateShortcut",BindingFlags.InvokeMethod,null,shell,new object[]{path});
            string target=shortcut.GetType().InvokeMember("TargetPath",BindingFlags.GetProperty,null,shortcut,null) as string;
            if(target!=null && Same(Canonical(target),Path.Combine(AppRoot,"EyeCopy.exe")))File.Delete(path);
        } finally {if(shortcut!=null && Marshal.IsComObject(shortcut))Marshal.FinalReleaseComObject(shortcut);if(shell!=null && Marshal.IsComObject(shell))Marshal.FinalReleaseComObject(shell);}
    }
    static int Main(string[] args) {
        string report=null,incoming=null; bool validate=false,reportSafe=false;
        try {
            for(int i=0;i<args.Length;i++) {
                if(args[i]=="--app-root")AppRoot=Canonical(args[++i]);else if(args[i]=="--report")report=Path.GetFullPath(args[++i]);
                else if(args[i]=="--incoming-manifest")incoming=Canonical(args[++i]);
                else if(args[i]=="--test-root")TestRoot=Canonical(args[++i]);else if(args[i]=="--cleanup")Execute=true;
                else if(args[i]=="--validate-install")validate=true;else if(args[i]!="--preview" && args[i]!="--confirmed")throw new InvalidOperationException("Unknown cleanup option.");
            }
            if(AppRoot==null)throw new InvalidOperationException("The application folder is required.");
            if(TestRoot!=null) { NoLinks(TestRoot);Marker(TestRoot,".eyecopy-uninstall-test.json",null); }
            if(report!=null) {
                string allowed=TestRoot??Canonical(Path.GetDirectoryName(System.Reflection.Assembly.GetExecutingAssembly().Location));
                report=Canonical(report);NoLinks(report);
                if(!Inside(report,allowed))throw new InvalidOperationException("The cleanup report escaped its isolated output folder.");
                reportSafe=true;
            }
            RegistryPath=TestRoot==null?@"Software\EyeCopy\Profiles":@"Software\EyeCopy\InstallerTests\"+KeyFor(TestRoot)+@"\Profiles";
            SafeRoot(AppRoot,true);
            if(validate) {
                ValidateInstall(incoming);
            } else {
                if(Execute && !args.Contains("--confirmed"))throw new InvalidOperationException("Cleanup requires explicit uninstall confirmation.");
                RequireClosed();Marker(AppRoot,".eyecopy-install.json","application");
                var profiles=Profiles();
                // Validate all paths, links and history before removing any data.
                bool requested=Execute;Execute=false;foreach(var p in profiles)Profile(p.Key,p.Value);Application();Execute=requested;Deleted=0;Kept.Clear();
                if(Execute) {foreach(var p in profiles)Profile(p.Key,p.Value);Application();if(TestRoot==null)OwnedShortcut(Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.Programs),"EyeCopy","EyeCopy.lnk"));}
            }
        } catch(Exception error) { Errors.Add(error.Message); }
        string result=Json.Serialize(new {success=Errors.Count==0,preview=!Execute,deletedFiles=Deleted,kept=Kept.ToArray(),errors=Errors.ToArray()});
        Console.WriteLine(result);if(reportSafe)try {File.WriteAllText(report,result,Encoding.UTF8);} catch(Exception error) {Console.Error.WriteLine(error.Message);return 2;}
        return Errors.Count==0?0:2;
    }
}
