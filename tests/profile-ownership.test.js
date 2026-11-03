const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const crypto = require('node:crypto');
const { registerProfile, markerName, applicationId } = require('../desktop/profile-ownership');

function fixture(t) {
  const output = path.resolve(__dirname, '../test-output/profile-ownership');
  fs.mkdirSync(output, { recursive: true });
  const directory = fs.mkdtempSync(path.join(output, 'fixture-'));
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  const calls = [], home = path.join(directory, 'home'), local = path.join(home, 'AppData', 'Local');
  fs.mkdirSync(local, { recursive: true });
  const options = { platform: 'win32', applicationRoot: path.join(directory, 'application'),
    environment: { USERPROFILE: home, LOCALAPPDATA: local, SystemRoot: 'C:\\Windows' },
    execute(command, args, opts) { calls.push({ command, args, opts }); } };
  return { directory, home, local, calls, options };
}

test('a fresh dedicated profile is marked and registered without a shell', t => {
  const f = fixture(t), profile = path.join(f.directory, 'dedicated-profile');
  assert.equal(registerProfile(profile, f.options).registered, true);
  assert.deepEqual(JSON.parse(fs.readFileSync(path.join(profile, markerName), 'utf8')), {
    applicationId, schema: 1, kind: 'profile', lastApplicationRoot: f.options.applicationRoot
  });
  assert.equal(f.calls.length, 3);
  assert(f.calls.every(c => c.opts.windowsHide && !c.opts.shell && c.args.includes('/f')));
  assert(f.calls[0].args[1].startsWith('HKCU\\Software\\EyeCopy\\Profiles\\'));
  assert.equal(f.calls[0].args[f.calls[0].args.indexOf('/d') + 1], profile);
});

test('an existing matching profile is registered after moving the application', t => {
  const f = fixture(t), profile = path.join(f.directory, 'dedicated');
  registerProfile(profile, f.options);
  fs.writeFileSync(path.join(profile, 'state.json'), '{}');
  f.options.applicationRoot = path.join(f.directory, 'moved-application');
  assert.equal(registerProfile(profile, f.options).registered, true);
  const application = f.calls.findLast(call => call.args.includes('ApplicationRoot'));
  assert.equal(application.args[application.args.indexOf('/d') + 1], f.options.applicationRoot);
});

test('an existing non-default unmarked profile is not claimed', t => {
  const f = fixture(t), profile = path.join(f.directory, 'another-application');
  fs.mkdirSync(profile); fs.writeFileSync(path.join(profile, 'state.json'), '{}');
  assert.equal(registerProfile(profile, f.options).reason, 'unmarked-existing-profile');
  assert.equal(fs.existsSync(path.join(profile, markerName)), false);
  assert.equal(f.calls.length, 0);
});

test('the standard existing EyeCopy profile can be enrolled', t => {
  const f = fixture(t), profile = path.join(f.local, 'EyeCopy');
  fs.mkdirSync(profile); fs.writeFileSync(path.join(profile, 'state.json'), '{}');
  assert.equal(registerProfile(profile, f.options).registered, true);
});

test('a foreign ownership marker is preserved and never registered', t => {
  const f = fixture(t), profile = path.join(f.directory, 'foreign'); fs.mkdirSync(profile);
  const value = JSON.stringify({ applicationId: 'com.example.other', schema: 1, kind: 'profile' });
  fs.writeFileSync(path.join(profile, markerName), value);
  assert.equal(registerProfile(profile, f.options).reason, 'foreign-profile-marker');
  assert.equal(fs.readFileSync(path.join(profile, markerName), 'utf8'), value);
  assert.equal(f.calls.length, 0);
});

test('broad directories and ancestors of the application are rejected', t => {
  const f = fixture(t);
  for (const profile of [f.home, f.local, path.join(f.home, 'Downloads'), f.directory, f.options.applicationRoot]) {
    assert.equal(registerProfile(profile, f.options).reason, 'unsafe-profile-location');
  }
  assert.equal(f.calls.length, 0);
});

test('system and application descendants and ambiguous paths are not enrolled', t => {
  const f = fixture(t);
  f.options.environment.ProgramFiles = path.join(f.directory, 'program-files');
  for (const profile of ['relative-profile', 'C:relative-profile', '\\relative-profile',
    'C:\\profile:stream', 'C:\\Windows\\temporary-profile',
    path.join(f.options.applicationRoot, 'data'), path.join(f.options.environment.ProgramFiles, 'profile')]) {
    assert.equal(registerProfile(profile, f.options).reason, 'unsafe-profile-location');
  }
  assert.equal(f.calls.length, 0);
});

test('a junction ancestor is rejected without writing through it', { skip: process.platform !== 'win32' }, t => {
  const f = fixture(t), real = path.join(f.directory, 'target'), link = path.join(f.directory, 'link');
  fs.mkdirSync(real); fs.symlinkSync(real, link, 'junction');
  assert.equal(registerProfile(path.join(link, 'profile'), f.options).reason, 'linked-profile-location');
  assert.equal(fs.existsSync(path.join(real, 'profile')), false);
});

test('a registry failure leaves the owned marker and returns a recoverable result', t => {
  const f = fixture(t), profile = path.join(f.directory, 'profile');
  f.options.execute = () => { throw new Error('Synthetic denied registry write'); };
  assert.equal(registerProfile(profile, f.options).reason, 'profile-registry-unavailable');
  assert.equal(fs.existsSync(path.join(profile, markerName)), true);
});

test('Unicode profile names have the same byte-stable identity as the uninstaller', t => {
  const f = fixture(t), profile = path.join(f.directory, '\u0130pek-\u00c9quipe');
  const result = registerProfile(profile, f.options);
  assert.equal(result.registered, true);
  const expected = crypto.createHash('sha256').update(profile.replace(/[A-Z]/g, c => c.toLowerCase())).digest('hex');
  assert.equal(result.registryKey.split('\\').at(-1), expected);
  assert.notEqual(expected, crypto.createHash('sha256').update(profile.toLowerCase()).digest('hex'));
});
