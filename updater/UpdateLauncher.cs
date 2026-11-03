// SPDX-License-Identifier: GPL-3.0-or-later
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Globalization;
using System.Linq;
using System.Web.Script.Serialization;
using System.Windows.Forms;
using Microsoft.Win32;

internal static class UpdateLauncher {
    const string ApplicationId = "com.eyecopy.desktop";

    internal sealed class RegisteredProfile {
        internal string Path, ApplicationRoot, LastUsedUtc;
        internal RegisteredProfile(string path, string applicationRoot, string lastUsedUtc = null) {
            Path = path; ApplicationRoot = applicationRoot; LastUsedUtc = lastUsedUtc;
        }
    }

    static string Canonical(string value) {
        if (String.IsNullOrWhiteSpace(value) || !System.Text.RegularExpressions.Regex.IsMatch(value, @"^[A-Za-z]:[\\/]") || value.IndexOf(':', 2) >= 0)
            throw new InvalidOperationException("An ordinary absolute local path is required.");
        return System.IO.Path.GetFullPath(value).TrimEnd(System.IO.Path.DirectorySeparatorChar, System.IO.Path.AltDirectorySeparatorChar);
    }

    static IEnumerable<RegisteredProfile> ReadRegisteredProfiles() {
        var records = new List<RegisteredProfile>();
        try {
            using (var hive = RegistryKey.OpenBaseKey(RegistryHive.CurrentUser, RegistryView.Registry64))
            using (var profiles = hive.OpenSubKey(@"Software\EyeCopy\Profiles", false)) {
                if (profiles != null) foreach (string name in profiles.GetSubKeyNames()) {
                    if (!System.Text.RegularExpressions.Regex.IsMatch(name, "^[a-f0-9]{64}$")) continue;
                    try {
                        using (var entry = profiles.OpenSubKey(name, false)) {
                            if (entry == null || entry.GetValueKind("Path") != RegistryValueKind.String || entry.GetValueKind("ApplicationRoot") != RegistryValueKind.String) continue;
                            records.Add(new RegisteredProfile(entry.GetValue("Path", null, RegistryValueOptions.DoNotExpandEnvironmentNames) as string,
                                entry.GetValue("ApplicationRoot", null, RegistryValueOptions.DoNotExpandEnvironmentNames) as string,
                                entry.GetValue("LastUsedUtc", null, RegistryValueOptions.DoNotExpandEnvironmentNames) as string));
                        }
                    } catch { /* An unreadable entry cannot select a profile. */ }
                }
            }
        } catch { /* No registry access is needed to open the normal profile. */ }
        return records;
    }

    static bool OwnedProfile(string directory) {
        NoLinks(directory);
        if (!Directory.Exists(directory)) return false;
        string markerPath = System.IO.Path.Combine(directory, ".eyecopy-profile.json");
        NoLinks(markerPath);
        if (!File.Exists(markerPath) || new FileInfo(markerPath).Length > 65536) return false;
        var parser = new JavaScriptSerializer { MaxJsonLength = 65536, RecursionLimit = 10 };
        var marker = parser.Deserialize<Dictionary<string,object>>(File.ReadAllText(markerPath));
        object id, schema, kind;
        return marker != null && marker.TryGetValue("applicationId", out id) && Object.Equals(id, ApplicationId) &&
            marker.TryGetValue("schema", out schema) && schema is int && (int)schema == 1 &&
            marker.TryGetValue("kind", out kind) && Object.Equals(kind, "profile");
    }

    static DateTimeOffset? UsedAt(string value) {
        DateTimeOffset parsed;
        if (value != null && System.Text.RegularExpressions.Regex.IsMatch(value, @"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,7})?Z$") &&
            DateTimeOffset.TryParse(value, CultureInfo.InvariantCulture, DateTimeStyles.AssumeUniversal | DateTimeStyles.AdjustToUniversal, out parsed)) return parsed;
        return null;
    }

    internal static string SelectProfile(string root, string explicitProfile, Func<IEnumerable<RegisteredProfile>> read) {
        // Respect a profile selected by the process that opened this launcher.
        if (!String.IsNullOrWhiteSpace(explicitProfile)) return explicitProfile;
        var candidates = new Dictionary<string, DateTimeOffset?>(StringComparer.OrdinalIgnoreCase);
        foreach (RegisteredProfile entry in read()) {
            try {
                if (!String.Equals(Canonical(entry.ApplicationRoot), Canonical(root), StringComparison.OrdinalIgnoreCase)) continue;
                string profile = Canonical(entry.Path);
                if (OwnedProfile(profile)) {
                    DateTimeOffset? used = UsedAt(entry.LastUsedUtc), previous;
                    if (!candidates.TryGetValue(profile, out previous) || used.HasValue && (!previous.HasValue || used.Value > previous.Value)) candidates[profile] = used;
                }
            } catch { /* Missing, foreign and redirected profiles are ignored. */ }
        }
        if (candidates.Count == 1) return candidates.Keys.First();
        var dated = candidates.Where(item => item.Value.HasValue).OrderByDescending(item => item.Value.Value).ToArray();
        if (dated.Length > 0 && (dated.Length == 1 || dated[0].Value.Value != dated[1].Value.Value)) return dated[0].Key;
        return null;
    }

    internal static void NoLinks(string value) {
        string current = Path.GetFullPath(value);
        while (!String.IsNullOrEmpty(current)) {
            if ((File.Exists(current) || Directory.Exists(current)) &&
                (File.GetAttributes(current) & FileAttributes.ReparsePoint) != 0)
                throw new InvalidOperationException("Linked application folders require manual review.");
            DirectoryInfo parent = Directory.GetParent(current);
            current = parent == null ? null : parent.FullName;
        }
    }

    internal static string ValidateApplication(string directory) {
        if (String.IsNullOrWhiteSpace(directory) || !System.Text.RegularExpressions.Regex.IsMatch(directory, @"^[A-Za-z]:[\\/]") || directory.IndexOf(':', 2) >= 0)
            throw new InvalidOperationException("Open this updater from an ordinary local EyeCopy application folder.");
        string root = Path.GetFullPath(directory);
        NoLinks(root);
        if (!Directory.Exists(root)) throw new InvalidOperationException("The EyeCopy application folder is missing.");
        string markerPath = Path.Combine(root, ".eyecopy-install.json");
        NoLinks(markerPath);
        if (!File.Exists(markerPath) || (File.GetAttributes(markerPath) & FileAttributes.Directory) != 0 || new FileInfo(markerPath).Length > 65536)
            throw new InvalidOperationException("The EyeCopy application marker is missing or invalid. Extract the complete Windows package again.");
        Dictionary<string,object> marker;
        try {
            var json = new JavaScriptSerializer { MaxJsonLength = 65536, RecursionLimit = 10 };
            marker = json.Deserialize<Dictionary<string,object>>(File.ReadAllText(markerPath));
        } catch {
            throw new InvalidOperationException("The EyeCopy application marker could not be read.");
        }
        object id, schema, kind;
        if (marker == null || !marker.TryGetValue("applicationId", out id) || !Object.Equals(id, ApplicationId) ||
            !marker.TryGetValue("schema", out schema) || !(schema is int) || (int)schema != 1 ||
            !marker.TryGetValue("kind", out kind) || !Object.Equals(kind, "application"))
            throw new InvalidOperationException("This folder is not marked as an EyeCopy application.");
        string executable = Path.Combine(root, "EyeCopy.exe");
        NoLinks(executable);
        if (!File.Exists(executable) || (File.GetAttributes(executable) & FileAttributes.Directory) != 0)
            throw new InvalidOperationException("EyeCopy.exe is missing. Extract the complete Windows package again.");
        using (FileStream input = File.OpenRead(executable)) {
            if (input.ReadByte() != 'M' || input.ReadByte() != 'Z')
                throw new InvalidOperationException("EyeCopy.exe is not a Windows application.");
        }
        return executable;
    }

    internal static void Launch(string root, Action<ProcessStartInfo> start) {
        Launch(root, start, ReadRegisteredProfiles, Environment.GetEnvironmentVariable("EYECOPY_DATA_DIR"));
    }

    internal static void Launch(string root, Action<ProcessStartInfo> start,
                               Func<IEnumerable<RegisteredProfile>> read, string explicitProfile) {
        string executable = ValidateApplication(root);
        var info = new ProcessStartInfo {
            FileName = executable,
            Arguments = "--updates",
            WorkingDirectory = Path.GetDirectoryName(executable),
            UseShellExecute = false
        };
        string profile = SelectProfile(root, explicitProfile, read);
        if (profile != null) info.EnvironmentVariables["EYECOPY_DATA_DIR"] = profile;
        start(info);
    }

    [STAThread]
    static int Main() {
        try {
            string root = Path.GetDirectoryName(Assembly.GetExecutingAssembly().Location);
            Launch(root, info => { using (Process process = Process.Start(info)) {} });
            return 0;
        } catch (Exception error) {
            Application.EnableVisualStyles();
            MessageBox.Show(error.Message, "EyeCopy Update", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 1;
        }
    }
}
