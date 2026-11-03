// SPDX-License-Identifier: GPL-3.0-or-later
using System;
using System.IO;
using System.Diagnostics;

internal static class LauncherTests {
    static int Passed;
    static void Require(bool value, string message) { if (!value) throw new Exception(message); }
    static void Rejected(string root) {
        bool called = false, rejected = false;
        try { UpdateLauncher.Launch(root, info => called = true, () => new UpdateLauncher.RegisteredProfile[0], null); } catch (InvalidOperationException) { rejected = true; }
        Require(rejected && !called, "Invalid application was not safely rejected."); Passed++;
    }
    static string Create(string baseDir, string name, string marker) {
        string root = Path.Combine(baseDir, name); Directory.CreateDirectory(root);
        if (marker != null) File.WriteAllText(Path.Combine(root, ".eyecopy-install.json"), marker);
        return root;
    }
    public static int Main(string[] args) {
        try {
            string root = Path.GetFullPath(args[0]);
            string valid = "{\"applicationId\":\"com.eyecopy.desktop\",\"schema\":1,\"kind\":\"application\"}";
            Rejected(Create(root, "missing-marker", null));
            Rejected(Create(root, "wrong-app", valid.Replace("com.eyecopy.desktop", "com.example.other")));
            Rejected(Create(root, "wrong-kind", valid.Replace("application\"}", "profile\"}")));
            Rejected(Create(root, "wrong-schema", valid.Replace("schema\":1", "schema\":2")));
            Rejected(Create(root, "malformed", "{damaged"));
            Rejected(Create(root, "oversized", new string(' ', 65537)));
            Rejected(Create(root, "missing-executable", valid));
            string directoryExe = Create(root, "directory-executable", valid);
            Directory.CreateDirectory(Path.Combine(directoryExe, "EyeCopy.exe")); Rejected(directoryExe);
            string badExe = Create(root, "non-executable", valid);
            File.WriteAllText(Path.Combine(badExe, "EyeCopy.exe"), "synthetic"); Rejected(badExe);
            string good = Create(root, "valid application with spaces", valid);
            File.WriteAllBytes(Path.Combine(good, "EyeCopy.exe"), new byte[]{77,90,0,0});
            int launches = 0;
            UpdateLauncher.Launch(good, info => {
                Require(info.FileName == Path.Combine(good, "EyeCopy.exe"), "Launcher changed the target.");
                Require(info.Arguments == "--updates", "Launcher changed the update switch.");
                Require(info.WorkingDirectory == good && !info.UseShellExecute, "Launcher used an unsafe shell or working directory.");
                launches++;
            }, () => new UpdateLauncher.RegisteredProfile[0], null);
            Require(launches == 1, "The valid application was not opened once."); Passed++;
            Rejected("C:relative"); Rejected("\\remote");
            if (Directory.Exists(Path.Combine(root, "junction-root"))) {
                Rejected(Path.Combine(root, "junction-root"));
                Rejected(Path.Combine(root, "junction-parent", "child"));
            }
            string profileMarker = valid.Replace("application\"}", "profile\"}");
            string first = Path.Combine(root, "profile-one"), second = Path.Combine(root, "profile-two"), foreign = Path.Combine(root, "foreign-profile");
            Directory.CreateDirectory(first); Directory.CreateDirectory(second); Directory.CreateDirectory(foreign);
            File.WriteAllText(Path.Combine(first, ".eyecopy-profile.json"), profileMarker);
            File.WriteAllText(Path.Combine(second, ".eyecopy-profile.json"), profileMarker);
            File.WriteAllText(Path.Combine(foreign, ".eyecopy-profile.json"), profileMarker.Replace("com.eyecopy.desktop", "com.example.other"));
            var a = new UpdateLauncher.RegisteredProfile(first, good);
            var b = new UpdateLauncher.RegisteredProfile(second, good);
            Require(UpdateLauncher.SelectProfile(good, null, () => new [] {a}) == first, "Sole registered profile was not selected."); Passed++;
            Require(UpdateLauncher.SelectProfile(good, null, () => new [] {new UpdateLauncher.RegisteredProfile(first, root)}) == null, "Another installation's profile was selected."); Passed++;
            Require(UpdateLauncher.SelectProfile(good, null, () => new [] {new UpdateLauncher.RegisteredProfile(foreign, good)}) == null, "Foreign profile was selected."); Passed++;
            Require(UpdateLauncher.SelectProfile(good, null, () => new [] {a,b}) == null, "Ambiguous undated profiles were guessed."); Passed++;
            a.LastUsedUtc = "2026-01-01T01:00:00.000Z"; b.LastUsedUtc = "2026-01-01T02:00:00.000Z";
            Require(UpdateLauncher.SelectProfile(good, null, () => new [] {a,b}) == second, "Most recently used profile was not selected."); Passed++;
            b.LastUsedUtc = a.LastUsedUtc;
            Require(UpdateLauncher.SelectProfile(good, null, () => new [] {a,b}) == null, "Tied usage timestamps were guessed."); Passed++;
            a.LastUsedUtc = "not-a-date"; b.LastUsedUtc = "2026-01-01T02:00:00.000Z";
            Require(UpdateLauncher.SelectProfile(good, null, () => new [] {a,b}) == second, "An invalid usage date displaced a valid date."); Passed++;
            Require(UpdateLauncher.SelectProfile(good, null, () => new [] {new UpdateLauncher.RegisteredProfile(Path.Combine(root,"profile-link"),good)}) == null, "A linked profile was selected."); Passed++;
            bool registryRead = false;
            Require(UpdateLauncher.SelectProfile(good, "explicit-profile", () => {registryRead = true; return new []{a,b};}) == "explicit-profile" && !registryRead, "Explicit caller profile was replaced or registry was read."); Passed++;
            string previous = Environment.GetEnvironmentVariable("EYECOPY_DATA_DIR");
            UpdateLauncher.Launch(good, info => Require(info.EnvironmentVariables["EYECOPY_DATA_DIR"] == second, "The selected profile was not scoped to the child."), () => new []{b}, null);
            Require(Environment.GetEnvironmentVariable("EYECOPY_DATA_DIR") == previous, "The launcher modified its global environment."); Passed++;
            Console.WriteLine("{\"passed\":true,\"checks\":" + Passed + ",\"realApplicationLaunches\":0}");
            return 0;
        } catch (Exception error) { Console.Error.WriteLine(error); return 1; }
    }
}
