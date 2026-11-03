'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const { createUpdateService, normalizeRepository, newerVersion } = require('../desktop/update-service');
const { createAppSettings } = require('../desktop/app-settings');

const repo = 'example/EyeCopy', version = '1.0.1';
const setupName = `Setup-EyeCopy-${version}.exe`;
const api = `https://api.github.com/repos/${repo}/releases/latest`;
const asset = name => `https://github.com/${repo}/releases/download/v${version}/${name}`;
const payload = Buffer.from('MZ-SYNTHETIC-INSTALLER-NOT-EXECUTABLE');
const sha = crypto.createHash('sha256').update(payload).digest('hex');
function release(overrides = {}) {
  return { tag_name: 'v' + version, draft: false, prerelease: false, assets: [
    { name: setupName, state: 'uploaded', size: payload.length, browser_download_url: asset(setupName), digest: 'sha256:' + sha },
    { name: 'SHA256SUMS.txt', state: 'uploaded', size: 100, browser_download_url: asset('SHA256SUMS.txt') }
  ], ...overrides };
}
function fixture(t, overrides = {}) {
  const base = path.resolve(__dirname, '../test-output/update-service'); fs.mkdirSync(base, { recursive: true });
  const directory = fs.mkdtempSync(path.join(base, 'fixture-')); t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  const calls = [], events = [], routes = new Map([
    [api, () => new Response(JSON.stringify(release()))],
    [asset('SHA256SUMS.txt'), () => new Response(sha + '  ' + setupName + '\n')],
    [asset(setupName), () => new Response(payload, { headers: { 'content-length': String(payload.length) } })]
  ]);
  const service = createUpdateService({ version: '1.0.0', dataDir: directory, repository: repo,
    fetch: async (url, options) => { calls.push({ url, options }); if (!routes.has(url)) throw new Error('Unexpected fixture URL'); return routes.get(url)(options); },
    onChange: value => events.push(value), ...overrides });
  t.after(() => service.dispose());
  return { directory, calls, events, routes, service };
}

test('repository settings accept only a public GitHub repository without credentials', () => {
  assert.equal(normalizeRepository(' https://github.com/example/EyeCopy.git/ '), 'example/EyeCopy');
  assert.equal(normalizeRepository(repo), repo); assert.equal(normalizeRepository(''), '');
  for (const value of ['http://github.com/example/EyeCopy', 'https://user:secret@github.com/example/EyeCopy', 'https://github.com:443/example/EyeCopy?x=1', 'https://evil.example/example/EyeCopy', 'https://github.com/example/EyeCopy/releases', 'example/..', 'localhost/repo/name']) assert.throws(() => normalizeRepository(value));
});

test('stable versions compare numerically and reject prerelease or ambiguous tags', () => {
  assert(newerVersion('v1.10.0', '1.9.9')); assert(!newerVersion('1.0.0', '1.0.0')); assert(!newerVersion('1.0.0', '2.0.0'));
  for (const value of ['latest', 'v1.0.1-beta', '01.0.1', '1.0', '9999999.0.0']) assert.throws(() => newerVersion(value, '1.0.0'));
});

test('an unconfigured repository makes zero network requests and creates no update cache', async t => {
  const f = fixture(t, { repository: '' });
  assert.equal((await f.service.check({ automatic: true })).status, 'unconfigured');
  assert.equal(f.calls.length, 0); assert.deepEqual(fs.readdirSync(f.directory), []);
});

test('a complete stable release is downloaded and SHA-verified without executing it', async t => {
  const f = fixture(t); assert.equal((await f.service.check()).status, 'available');
  assert.equal((await f.service.download()).status, 'ready');
  const ready = await f.service.installer();
  assert.equal(ready.version, version); assert.deepEqual(fs.readFileSync(ready.path), payload);
  assert.equal(f.calls.length, 3); assert(f.calls.every(c => c.options.credentials === 'omit' && c.options.redirect === 'manual' && !c.options.headers.Authorization));
});

test('current and older stable releases never offer a downgrade', async t => {
  const f = fixture(t); f.routes.set(api, () => new Response(JSON.stringify(release({ tag_name: 'v1.0.0' }))));
  assert.equal((await f.service.check()).status, 'current'); await assert.rejects(f.service.download(), /available update/);
});

test('drafts, prereleases and missing checksums do not become install candidates', async t => {
  const f = fixture(t);
  for (const value of [release({ draft: true }), release({ prerelease: true }), release({ assets: release().assets.slice(0, 1) })]) {
    f.routes.set(api, () => new Response(JSON.stringify(value))); assert.equal((await f.service.check()).status, 'error');
    await assert.rejects(f.service.installer(), /verify/);
  }
});

test('a release cannot redirect an executable download to an unrelated host', async t => {
  const f = fixture(t); await f.service.check();
  f.routes.set(asset(setupName), () => new Response(null, { status: 302, headers: { location: 'https://untrusted.example/update.exe' } }));
  await assert.rejects(f.service.download(), /outside GitHub/); assert(!f.calls.some(c => c.url.includes('untrusted.example')));
});

test('GitHub CDN redirects are accepted only after the original repository asset URL', async t => {
  const f = fixture(t); await f.service.check(); const cdn = 'https://release-assets.githubusercontent.com/github-production-release-asset/synthetic?token=fixture';
  f.routes.set(asset(setupName), () => new Response(null, { status: 302, headers: { location: cdn } }));
  f.routes.set(cdn, () => new Response(payload));
  assert.equal((await f.service.download()).status, 'ready');
});

test('a checksum mismatch never produces an executable ready for launch', async t => {
  const f = fixture(t); await f.service.check(); const corrupt = Buffer.from(payload); corrupt[5] ^= 0xff; f.routes.set(asset(setupName), () => new Response(corrupt));
  await assert.rejects(f.service.download(), /verification failed/); await assert.rejects(f.service.installer(), /verify/);
  assert.equal(fs.readdirSync(path.join(f.directory, 'updates'), { recursive: true }).some(name => name.endsWith('.part') || name.endsWith('.exe')), false);
});

test('duplicate checksums and disagreement with the GitHub asset digest are rejected', async t => {
  const f = fixture(t); await f.service.check();
  f.routes.set(asset('SHA256SUMS.txt'), () => new Response(`${sha}  ${setupName}\n${sha}  ${setupName}\n`));
  await assert.rejects(f.service.download(), /unique/);
  f.routes.set(asset('SHA256SUMS.txt'), () => new Response(`${'0'.repeat(64)}  ${setupName}\n`));
  await assert.rejects(f.service.download(), /disagree/); assert(!f.calls.some(c => c.url === asset(setupName)));
});

test('an installer modified after verification is rejected before launch', async t => {
  const f = fixture(t); await f.service.check(); await f.service.download(); const ready = await f.service.installer();
  fs.writeFileSync(ready.path, Buffer.alloc(payload.length)); await assert.rejects(f.service.installer(), /changed/);
});

test('changing repositories revokes a previously verified installer', async t => {
  const f = fixture(t); await f.service.check(); await f.service.download(); f.service.configure('other/Project');
  assert.equal(f.service.get().repository, 'other/Project'); await assert.rejects(f.service.installer(), /verify/);
});

test('late responses from a previous repository cannot replace the new status', async t => {
  const f = fixture(t); let resolve; f.routes.set(api, () => new Promise(done => { resolve = done; }));
  const checking = f.service.check(); f.service.configure('other/Project'); resolve(new Response(JSON.stringify(release()))); await checking;
  assert.equal(f.service.get().repository, 'other/Project'); assert.equal(f.service.get().status, 'idle');
});

test('disabling automatic checks cancels an in-flight automatic response', async t => {
  const f = fixture(t); let resolve; f.routes.set(api, () => new Promise(done => { resolve = done; }));
  const checking = f.service.check({ automatic: true }); f.service.cancelAutomatic(); resolve(new Response(JSON.stringify(release()))); await checking;
  assert.equal(f.service.get().status, 'idle');
});

test('a linked cache folder cannot receive an update installer', { skip: process.platform !== 'win32' }, async t => {
  const f = fixture(t), outside = path.join(f.directory, 'outside'); fs.mkdirSync(outside); fs.symlinkSync(outside, path.join(f.directory, 'updates'), 'junction');
  await f.service.check(); await assert.rejects(f.service.download(), /linked/); assert.deepEqual(fs.readdirSync(outside), []);
});

test('repository and automatic-check preferences persist with existing settings', t => {
  const f = fixture(t), settings = createAppSettings(f.directory, { defaultRepository: 'default/Project' });
  assert.equal(settings.get().updateRepository, 'default/Project'); settings.update({ updateRepository: 'https://github.com/example/EyeCopy', autoCheckUpdates: false });
  assert.equal(createAppSettings(f.directory).get().updateRepository, repo); assert.equal(createAppSettings(f.directory).get().autoCheckUpdates, false);
  settings.update({ updateRepository: '' }); assert.equal(createAppSettings(f.directory, { defaultRepository: 'default/Project' }).get().updateRepository, '');
  assert.throws(() => settings.update({ updateRepository: 'https://secret@github.com/a/b' }));
});

test('the daily automatic-check interval survives restarting EyeCopy', async t => {
  const f = fixture(t); await f.service.check({ automatic: true }); const count = f.calls.length;
  const restarted = createUpdateService({ version: '1.0.0', dataDir: f.directory, repository: repo,
    fetch: async () => { throw new Error('Daily cooldown should not issue another request'); } });
  t.after(() => restarted.dispose()); assert.equal((await restarted.check({ automatic: true })).status, 'idle'); assert.equal(f.calls.length, count);
});

test('late download chunks after changing repository do not restore an old ready state', async t => {
  const f = fixture(t); await f.service.check(); let output;
  f.routes.set(asset(setupName), () => new Response(new ReadableStream({ start(controller) { output = controller; } })));
  const pending = f.service.download();
  for (let n = 0; n < 100 && !output; n++) await new Promise(resolve => setTimeout(resolve, 2));
  assert(output); f.service.configure('other/Project'); output.enqueue(payload); output.close();
  await assert.rejects(pending, /cancelled/); assert.equal(f.service.get().repository, 'other/Project'); assert.equal(f.service.get().status, 'idle');
  await assert.rejects(f.service.installer(), /verify/);
});
