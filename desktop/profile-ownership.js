const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const crypto = require('node:crypto');
const { execFileSync } = require('node:child_process');

const applicationId = 'com.eyecopy.desktop';
const markerName = '.eyecopy-profile.json';
const registryBase = 'HKCU\\Software\\EyeCopy\\Profiles';
const same = (a, b) => path.resolve(a).toLowerCase() === path.resolve(b).toLowerCase();
const contains = (parent, child) => same(parent, child) || path.resolve(child).toLowerCase().startsWith(path.resolve(parent).toLowerCase() + path.sep);

function hasLinkedAncestor(directory, filesystem = fs) {
  let current = path.resolve(directory);
  while (true) {
    try { if (filesystem.lstatSync(current).isSymbolicLink()) return true; }
    catch (error) { if (error.code !== 'ENOENT') throw error; }
    const parent = path.dirname(current);
    if (parent === current) return false;
    current = parent;
  }
}

function registerProfile(directory, options = {}) {
  const filesystem = options.filesystem || fs;
  const execute = options.execute || execFileSync;
  const environment = options.environment || process.env;
  const platform = options.platform || process.platform;
  if (platform !== 'win32') return { registered: false, reason: 'unsupported-platform' };
  const home = environment.USERPROFILE || os.homedir();
  const local = environment.LOCALAPPDATA || path.join(home, 'AppData', 'Local');
  const applicationRoot = path.resolve(options.applicationRoot || __dirname);
  if (typeof directory !== 'string' || !/^[A-Za-z]:[\\/]/.test(directory) || /[:]/.test(directory.slice(2))) {
    return { registered: false, reason: 'unsafe-profile-location' };
  }
  const profile = path.resolve(directory);
  const protectedRoots = [home, local, environment.APPDATA,
    environment.SystemRoot, environment.ProgramFiles, environment['ProgramFiles(x86)'], environment.ProgramData,
    path.join(home, 'Desktop'), path.join(home, 'Documents'), path.join(home, 'Downloads'),
    path.join(home, 'Pictures'), path.join(home, 'Videos'), applicationRoot,
    ...(options.protectedRoots || [])].filter(Boolean);
  const blockedTrees = [environment.SystemRoot, environment.ProgramFiles,
    environment['ProgramFiles(x86)'], environment.ProgramData, applicationRoot].filter(Boolean);
  if (profile.startsWith('\\\\') || profile === path.parse(profile).root ||
      blockedTrees.some(root => contains(root, profile)) ||
      protectedRoots.some(root => contains(profile, root))) {
    return { registered: false, reason: 'unsafe-profile-location' };
  }
  if (hasLinkedAncestor(profile, filesystem)) return { registered: false, reason: 'linked-profile-location' };
  filesystem.mkdirSync(profile, { recursive: true });
  const markerPath = path.join(profile, markerName);
  let marker;
  if (filesystem.existsSync(markerPath)) {
    const stat = filesystem.lstatSync(markerPath);
    if (!stat.isFile() || stat.isSymbolicLink() || stat.size > 65536) return { registered: false, reason: 'invalid-profile-marker' };
    try { marker = JSON.parse(filesystem.readFileSync(markerPath, 'utf8')); }
    catch { return { registered: false, reason: 'invalid-profile-marker' }; }
    if (marker.applicationId !== applicationId || marker.schema !== 1 || marker.kind !== 'profile') {
      return { registered: false, reason: 'foreign-profile-marker' };
    }
  } else {
    // An arbitrary existing folder may belong to another application. Only
    // fresh dedicated folders and EyeCopy's standard profile can be enrolled.
    if (!same(profile, path.join(local, 'EyeCopy')) && filesystem.readdirSync(profile).length) {
      return { registered: false, reason: 'unmarked-existing-profile' };
    }
    marker = { applicationId, schema: 1, kind: 'profile', lastApplicationRoot: applicationRoot };
    filesystem.writeFileSync(markerPath, JSON.stringify(marker, null, 2), { encoding: 'utf8', flag: 'wx' });
  }
  // Keep registry identities consistent with the .NET uninstaller regardless
  // of the host's Unicode casing tables.
  const identityPath = profile.replace(/[A-Z]/g, letter => letter.toLowerCase());
  const identifier = crypto.createHash('sha256').update(identityPath).digest('hex');
  const key = (options.registryBase || registryBase) + '\\' + identifier;
  const registry = path.join(environment.SystemRoot || 'C:\\Windows', 'System32', 'reg.exe');
  const run = args => execute(registry, args, { windowsHide: true, timeout: 5000, stdio: 'pipe' });
  try {
    run(['ADD', key, '/v', 'Path', '/t', 'REG_SZ', '/d', profile, '/f']);
    run(['ADD', key, '/v', 'ApplicationRoot', '/t', 'REG_SZ', '/d', applicationRoot, '/f']);
    run(['ADD', key, '/v', 'LastUsedUtc', '/t', 'REG_SZ', '/d', new Date().toISOString(), '/f']);
    return { registered: true, registryKey: key };
  } catch {
    return { registered: false, reason: 'profile-registry-unavailable' };
  }
}

module.exports = { registerProfile, hasLinkedAncestor, applicationId, markerName, registryBase };
