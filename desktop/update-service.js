'use strict';
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');

const DOWNLOAD_HOSTS = new Set(['github.com', 'release-assets.githubusercontent.com', 'objects.githubusercontent.com', 'github-releases.githubusercontent.com']);
const MAX_SETUP = 2 * 1024 * 1024 * 1024;

function normalizeRepository(value) {
  if (typeof value !== 'string') throw new Error('Enter a GitHub repository URL or owner/repository.');
  let text = value.trim();
  if (!text) return '';
  if (/^https:\/\//i.test(text)) {
    let url;
    try { url = new URL(text); } catch { throw new Error('Enter a valid GitHub repository URL.'); }
    if (url.protocol !== 'https:' || url.hostname !== 'github.com' || url.port || url.username || url.password || url.search || url.hash) throw new Error('Use a public https://github.com/owner/repository URL.');
    text = url.pathname.replace(/^\//, '').replace(/\/$/, '');
  }
  text = text.replace(/\.git$/, '');
  if (!/^[A-Za-z0-9][A-Za-z0-9-]{0,38}\/[A-Za-z0-9_.-]{1,100}$/.test(text) || ['.', '..'].includes(text.split('/')[1])) throw new Error('Use a public GitHub owner/repository without credentials or extra paths.');
  return text;
}

function parseVersion(value) {
  const match = typeof value === 'string' && /^v?(0|[1-9]\d{0,4})\.(0|[1-9]\d{0,4})\.(0|[1-9]\d{0,4})$/.exec(value);
  if (!match || match.slice(1).some(n => Number(n) > 65535)) throw new Error('The release tag must be a stable version such as v1.0.1.');
  return match.slice(1).map(Number);
}
function newerVersion(candidate, current) {
  const a = parseVersion(candidate), b = parseVersion(current);
  for (let at = 0; at < 3; at++) if (a[at] !== b[at]) return a[at] > b[at];
  return false;
}

function validateUrl(value, kind, repository, tag, filename, redirected = false) {
  const url = new URL(value);
  if (url.protocol !== 'https:' || url.username || url.password || url.port || url.hash) throw new Error('An update URL is not a trusted HTTPS download.');
  if (kind === 'api') {
    if (url.hostname !== 'api.github.com' || url.pathname.toLowerCase() !== `/repos/${repository}/releases/latest`.toLowerCase() || url.search) throw new Error('The release API redirected outside the configured repository.');
  } else if (!redirected) {
    let expected;
    try { expected = decodeURIComponent(url.pathname); } catch { throw new Error('Invalid release download URL.'); }
    if (url.hostname !== 'github.com' || expected.toLowerCase() !== `/${repository}/releases/download/${tag}/${filename}`.toLowerCase() || url.search) throw new Error('The release asset does not belong to the configured repository.');
  } else if (!DOWNLOAD_HOSTS.has(url.hostname)) throw new Error('The update download redirected outside GitHub.');
  return url.href;
}

function assertUnlinked(value) {
  let cursor = path.resolve(value);
  while (true) {
    try { if (fs.lstatSync(cursor).isSymbolicLink()) throw new Error('An update folder is linked to another location.'); }
    catch (error) { if (error.code !== 'ENOENT') throw error; }
    const parent = path.dirname(cursor); if (cursor === parent) break; cursor = parent;
  }
}

function createUpdateService({ version, dataDir, repository = '', fetch: fetcher = globalThis.fetch, onChange = () => {}, now = Date.now } = {}) {
  parseVersion(version);
  let selectedRepository = normalizeRepository(repository), generation = 0, controller = null, operation = null;
  function lastCheck(repo) {
    try {
      const receipt = path.join(dataDir, 'updates', 'check.json'); assertUnlinked(receipt);
      const value = JSON.parse(fs.readFileSync(receipt, 'utf8'));
      return value.repository?.toLowerCase() === repo.toLowerCase() && Number.isSafeInteger(value.checkedAt) && value.checkedAt >= 0 && value.checkedAt <= now() ? value.checkedAt : null;
    } catch { return null; }
  }
  function saveCheck(repo, checkedAt) {
    const directory = path.join(dataDir, 'updates'); assertUnlinked(directory); fs.mkdirSync(directory, { recursive: true });
    const target = path.join(directory, 'check.json'), temporary = path.join(directory, crypto.randomUUID() + '.tmp');
    assertUnlinked(target);
    try { fs.writeFileSync(temporary, JSON.stringify({ repository: repo, checkedAt }), { encoding: 'utf8', flag: 'wx' }); fs.renameSync(temporary, target); }
    finally { try { fs.unlinkSync(temporary); } catch {} }
  }
  let candidate = null, ready = null, lastAutomatic = lastCheck(selectedRepository);
  let state = { status: selectedRepository ? 'idle' : 'unconfigured', currentVersion: version, repository: selectedRepository, message: selectedRepository ? 'Check for published updates.' : 'Set a GitHub repository to enable updates.' };
  const publish = patch => { state = { ...state, ...patch }; onChange({ ...state }); return { ...state }; };
  const current = () => ({ ...state });
  const reset = () => ({ status: selectedRepository ? 'idle' : 'unconfigured', currentVersion: version, repository: selectedRepository, message: selectedRepository ? 'Check for published updates.' : 'Set a GitHub repository to enable updates.', availableVersion: null, downloadedBytes: 0, totalBytes: 0 });

  async function response(url, kind, descriptor, signal) {
    let currentUrl = validateUrl(url, kind, descriptor.repository, descriptor.tag, descriptor.filename);
    for (let hop = 0; hop <= 5; hop++) {
      const reply = await fetcher(currentUrl, { redirect: 'manual', signal, credentials: 'omit', headers: { 'User-Agent': `EyeCopy/${version}`, Accept: kind === 'api' ? 'application/vnd.github+json' : 'application/octet-stream', 'X-GitHub-Api-Version': '2026-03-10' } });
      if ([301, 302, 303, 307, 308].includes(reply.status)) {
        const location = reply.headers.get('location'); await reply.body?.cancel();
        if (!location || hop === 5) throw new Error('The update download redirected too many times.');
        currentUrl = validateUrl(new URL(location, currentUrl).href, kind, descriptor.repository, descriptor.tag, descriptor.filename, kind !== 'api');
        continue;
      }
      if (reply.status === 404 && kind === 'api') throw new Error('No published stable release was found. Check the repository setting.');
      if ([403, 429].includes(reply.status)) throw new Error('GitHub is limiting update requests. Try again later.');
      if (reply.status !== 200) throw new Error('GitHub could not provide the update. Try again later.');
      return reply;
    }
    throw new Error('The update download could not be opened.');
  }

  async function bytes(url, kind, descriptor, limit, signal) {
    const reply = await response(url, kind, descriptor, signal), chunks = [];
    let total = 0;
    try {
      for await (const chunk of reply.body) { total += chunk.byteLength; if (total > limit) throw new Error('The update metadata is larger than expected.'); chunks.push(Buffer.from(chunk)); }
      return Buffer.concat(chunks).toString('utf8');
    } catch (error) { try { await reply.body.cancel(); } catch {} throw error; }
  }

  function configure(value) {
    const normalized = normalizeRepository(value);
    if (normalized.toLowerCase() === selectedRepository.toLowerCase()) return current();
    generation++; controller?.abort(); controller = null; operation = null; candidate = ready = null;
    selectedRepository = normalized; lastAutomatic = lastCheck(selectedRepository); return publish(reset());
  }

  async function check({ automatic = false } = {}) {
    if (!selectedRepository) return publish(reset());
    if (operation) return current();
    if (automatic && (['available', 'ready'].includes(state.status) || lastAutomatic !== null && now() - lastAutomatic < 24 * 3600 * 1000)) return current();
    if (automatic) lastAutomatic = now();
    const token = generation, repo = selectedRepository, task = new AbortController(); controller = task;
    const signal = AbortSignal.any([task.signal, AbortSignal.timeout(30000)]);
    operation = automatic ? 'automatic-check' : 'check'; candidate = ready = null;
    publish({ status: 'checking', availableVersion: null, message: 'Checking GitHub Releases…', downloadedBytes: 0, totalBytes: 0 });
    try {
      if (automatic) saveCheck(repo, lastAutomatic);
      const text = await bytes(`https://api.github.com/repos/${repo}/releases/latest`, 'api', { repository: repo }, 1024 * 1024, signal);
      if (token !== generation) return current();
      const release = JSON.parse(text), tuple = parseVersion(release.tag_name), nextVersion = tuple.join('.');
      if (release.draft !== false || release.prerelease !== false) throw new Error('Only published stable releases can be installed.');
      if (!newerVersion(nextVersion, version)) return publish({ status: 'current', message: 'EyeCopy is up to date.', availableVersion: null });
      if (!Array.isArray(release.assets) || release.assets.length > 1000) throw new Error('The release has no valid Windows assets.');
      const filename = `Setup-EyeCopy-${nextVersion}.exe`, matches = name => release.assets.filter(a => a?.name === name);
      const installers = matches(filename), sums = matches('SHA256SUMS.txt');
      if (installers.length !== 1 || sums.length !== 1) throw new Error('The release needs its Windows setup and SHA256SUMS.txt assets.');
      const asset = installers[0], checksum = sums[0];
      if (asset.state !== 'uploaded' || checksum.state !== 'uploaded' || !Number.isSafeInteger(asset.size) || asset.size <= 0 || asset.size > MAX_SETUP || !Number.isSafeInteger(checksum.size) || checksum.size <= 0 || checksum.size > 256 * 1024) throw new Error('The release assets have invalid sizes or are not ready.');
      const descriptor = { repository: repo, tag: release.tag_name, version: nextVersion, filename, size: asset.size,
        url: validateUrl(asset.browser_download_url, 'asset', repo, release.tag_name, filename),
        checksumUrl: validateUrl(checksum.browser_download_url, 'asset', repo, release.tag_name, 'SHA256SUMS.txt'),
        digest: asset.digest || null };
      candidate = descriptor;
      return publish({ status: 'available', availableVersion: nextVersion, message: `EyeCopy ${nextVersion} is available.`, totalBytes: asset.size });
    } catch (error) {
      if (token === generation) return publish({ status: 'error', message: task.signal.aborted ? 'Update check cancelled.' : error.name === 'TimeoutError' ? 'Update check timed out. Try again later.' : error instanceof SyntaxError ? 'GitHub returned invalid release metadata.' : error.message });
      return current();
    } finally { if (token === generation) { operation = null; controller = null; } }
  }

  async function download() {
    if (operation) throw new Error('An update check or download is already running.');
    if (!candidate || candidate.repository.toLowerCase() !== selectedRepository.toLowerCase()) throw new Error('Check for an available update first.');
    const descriptor = { ...candidate }, token = generation, task = new AbortController(); controller = task; operation = 'download';
    const signal = AbortSignal.any([task.signal, AbortSignal.timeout(15 * 60 * 1000)]);
    let partial, directory;
    publish({ status: 'downloading', message: 'Downloading and verifying the update…', downloadedBytes: 0, totalBytes: descriptor.size });
    try {
      const checksums = await bytes(descriptor.checksumUrl, 'asset', { ...descriptor, filename: 'SHA256SUMS.txt' }, 256 * 1024, signal);
      if (token !== generation || signal.aborted) throw new Error('Update download cancelled.');
      const entries = checksums.split(/\r?\n/).map(line => /^([a-f0-9]{64})[ \t]+\*?([^\r\n]+)$/i.exec(line)).filter(m => m && m[2] === descriptor.filename);
      if (entries.length !== 1) throw new Error('The update has no unique SHA-256 checksum.');
      const expected = entries[0][1].toLowerCase();
      if (descriptor.digest && descriptor.digest.toLowerCase() !== `sha256:${expected}`) throw new Error('GitHub and the release checksums disagree.');
      assertUnlinked(dataDir); const updates = path.join(dataDir, 'updates'); assertUnlinked(updates);
      await fs.promises.mkdir(updates, { recursive: true });
      directory = path.join(updates, crypto.randomUUID()); await fs.promises.mkdir(directory);
      partial = path.join(directory, descriptor.filename + '.part');
      const reply = await response(descriptor.url, 'asset', descriptor, signal), length = Number(reply.headers.get('content-length'));
      if (token !== generation || signal.aborted) throw new Error('Update download cancelled.');
      if (length && length !== descriptor.size) throw new Error('The update download size changed. Check for updates again.');
      const hash = crypto.createHash('sha256'), file = await fs.promises.open(partial, 'wx');
      let total = 0, lastPaint = 0;
      try {
        for await (const chunk of reply.body) {
          if (token !== generation || signal.aborted) throw new Error('Update download cancelled.');
          total += chunk.byteLength; if (total > descriptor.size || total > MAX_SETUP) throw new Error('The update download exceeded its expected size.');
          const buffer = Buffer.from(chunk); hash.update(buffer);
          let written = 0;
          while (written < buffer.length) { const result = await file.write(buffer, written, buffer.length - written); if (!result.bytesWritten) throw new Error('The update could not be saved completely.'); written += result.bytesWritten; }
          if (token !== generation || signal.aborted) throw new Error('Update download cancelled.');
          if (now() - lastPaint >= 100 || total === descriptor.size) { publish({ downloadedBytes: total }); lastPaint = now(); }
        }
      } finally { await file.close(); }
      if (token !== generation || signal.aborted) throw new Error('Update download cancelled.');
      if (total !== descriptor.size || hash.digest('hex') !== expected) throw new Error('Update verification failed. The installer was not opened.');
      const final = path.join(directory, descriptor.filename); await fs.promises.rename(partial, final); partial = final;
      if (token !== generation || signal.aborted) throw new Error('Update download cancelled.');
      ready = { ...descriptor, path: final, sha256: expected, generation: token };
      partial = null;
      return publish({ status: 'ready', message: `EyeCopy ${descriptor.version} is verified and ready to install.`, downloadedBytes: total });
    } catch (error) {
      if (token === generation) publish({ status: 'error', message: task.signal.aborted ? 'Update download cancelled.' : error.name === 'TimeoutError' ? 'Update download timed out. Try again later.' : error.message });
      throw error;
    } finally {
      if (partial) { try { await fs.promises.unlink(partial); } catch {} }
      if (directory && !ready?.path?.startsWith(directory + path.sep)) { try { await fs.promises.rmdir(directory); } catch {} }
      if (token === generation) { operation = null; controller = null; }
    }
  }

  async function installer() {
    if (!ready || ready.generation !== generation || ready.repository.toLowerCase() !== selectedRepository.toLowerCase()) throw new Error('Download and verify the current update first.');
    const expected = { ...ready };
    assertUnlinked(expected.path);
    const stat = await fs.promises.lstat(expected.path); if (!stat.isFile() || stat.size !== expected.size) throw new Error('The verified installer is no longer available.');
    const hash = crypto.createHash('sha256'); for await (const chunk of fs.createReadStream(expected.path)) hash.update(chunk);
    if (hash.digest('hex') !== expected.sha256) throw new Error('The downloaded installer changed. Check for updates again.');
    const file = await fs.promises.open(expected.path, 'r');
    try { const header = Buffer.alloc(2); await file.read(header, 0, 2, 0); if (header.toString('ascii') !== 'MZ') throw new Error('The release asset is not a Windows installer.'); }
    finally { await file.close(); }
    if (expected.generation !== generation || expected.repository.toLowerCase() !== selectedRepository.toLowerCase()) throw new Error('Update settings changed. Check the selected repository again.');
    return { path: expected.path, version: expected.version, repository: expected.repository };
  }

  function cancel() { generation++; controller?.abort(); controller = null; operation = null; ready = null; return publish(candidate ? { ...reset(), status: 'available', availableVersion: candidate.version, totalBytes: candidate.size, message: `EyeCopy ${candidate.version} is available.` } : reset()); }
  return { get: current, configure, check, download, installer, cancel,
    cancelAutomatic() { return operation === 'automatic-check' ? cancel() : current(); },
    busy: () => Boolean(operation), dispose() { generation++; controller?.abort(); } };
}

module.exports = { createUpdateService, normalizeRepository, parseVersion, newerVersion, validateUrl };
