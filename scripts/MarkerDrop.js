'use strict';
// Electron drags a tiny identified local text ticket. Exact registered Shell copy
// notifications identify the chosen folder for the ordinary transfer engine.
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const { spawn } = require('node:child_process');
const readline = require('node:readline');
function markerPayload(basename) {
  // Legacy names remain exact-nonce compatibility for already owned tickets;
  // every newly prepared ticket uses the current brand.
  const match = /^_(EyeCopy)_drop_([0-9a-f]{32})\.(transfer|txt)$/.exec(basename);
  if (!match) throw new Error('Invalid transfer ticket name.');
  return match[3] === 'transfer' ? Buffer.alloc(0) : Buffer.from(`${match[1]} transfer ticket v1\n${match[2]}\n`, 'ascii');
}
function ownedPayload(file, basename) {
  let fd;
  try {
    const expected = markerPayload(basename), stat = fs.lstatSync(file); if (!stat.isFile() || stat.isSymbolicLink() || stat.size !== expected.length) return false;
    fd = fs.openSync(file, 'r'); if (fs.fstatSync(fd).size !== expected.length) return false;
    const actual = Buffer.alloc(expected.length); return fs.readSync(fd, actual, 0, actual.length, 0) === expected.length && actual.equals(expected);
  } catch { return false; } finally { if (fd !== undefined) fs.closeSync(fd); }
}

async function buildMarkerProbe(dataDir, { preferBundled = true } = {}) {
  const bundled = path.join(__dirname, '..', 'assets', 'native', 'MarkerDropProbe.exe');
  if (preferBundled && fs.existsSync(bundled)) return bundled;
  const source = path.join(__dirname, 'MarkerDropProbe.cs');
  const hash = crypto.createHash('sha256').update(await fs.promises.readFile(source)).digest('hex').slice(0, 20);
  const directory = path.join(dataDir, 'marker-probe', hash), executable = path.join(directory, 'MarkerDropProbe.exe');
  if (fs.existsSync(executable)) return executable;
  await fs.promises.mkdir(directory, { recursive: true });
  const compiler = path.join(process.env.SystemRoot || 'C:\\Windows', 'Microsoft.NET', 'Framework64', 'v4.0.30319', 'csc.exe');
  await new Promise((resolve, reject) => {
    const child = spawn(compiler, ['/nologo', '/target:exe', '/platform:x64', '/optimize+', '/reference:Microsoft.CSharp.dll', '/reference:System.Windows.Forms.dll', '/reference:System.Web.Extensions.dll', `/out:${executable}`, source], { windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'] });
    let output = ''; child.stdout.on('data', b => { output += String(b).slice(0, 6000); }); child.stderr.on('data', b => { output += String(b).slice(0, 6000); });
    child.on('error', reject); child.on('exit', code => code === 0 ? resolve() : reject(new Error('Marker probe compilation failed: ' + output.slice(0, 1500))));
  });
  return executable;
}
function claimMarker(executable, folder, basename) {
  return new Promise((resolve, reject) => {
    const child = spawn(executable, ['--claim', folder, basename], { windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'] });
    let output = ''; const timeout = setTimeout(() => { child.kill(); reject(new Error('Marker claim timed out.')); }, 5000);
    child.stdout.on('data', b => { output += String(b).slice(0, 4096); }); child.stderr.on('data', () => {});
    child.on('error', error => { clearTimeout(timeout); reject(error); });
    child.on('exit', code => { clearTimeout(timeout); try { resolve(code === 0 && JSON.parse(output.trim()).claimed === true); } catch { reject(new Error('Marker claim returned invalid data.')); } });
  });
}

class MarkerDrop {
  constructor({ dataDir, sourceDir = path.join(dataDir, 'marker-sources'), executable, onEvent = () => {}, timeoutMs = 90000, claim = claimMarker }) {
    this.sourceDir = path.resolve(sourceDir); this.executable = executable; this.onEvent = onEvent; this.timeoutMs = timeoutMs; this.claim = claim;
    this.jobs = new Map(); this.folders = new Map(); this.openFolders = new Set(); this.retired = new Map(); this.registrations = new Map(); this.child = null; this.startup = null; this.notificationReady = false; this.closed = false;
    fs.mkdirSync(this.sourceDir, { recursive: true });
  }
  async start() {
    if (this.closed) throw new Error('Marker detector is closed.');
    if (this.startup) return this.startup;
    this.startup = new Promise((resolve, reject) => {
      const child = this.child = spawn(this.executable, ['--watch'], { windowsHide: true, stdio: ['pipe', 'pipe', 'pipe'] });
      let ready = false; const timeout = setTimeout(() => { child.kill(); reject(new Error('Explorer folder detector did not start.')); }, 8000);
      readline.createInterface({ input: child.stdout }).on('line', line => {
        try {
          const message = JSON.parse(line);
          if (message.event === 'ready' && message.notifications === true) { ready = true; this.notificationReady = true; clearTimeout(timeout); resolve(); }
          else if (message.event === 'marker-registered') {
            const pending = this.registrations.get(message.basename); if (!pending) return;
            clearTimeout(pending.timer); this.registrations.delete(message.basename);
            const job = this.jobs.get(pending.id);
            if (message.accepted === true && job) { job.registered = true; this.onEvent({ event: 'marker-registered', id: job.id, basename: job.basename, registeredCount: Number(message.registeredCount) || 0 }); pending.resolve(); }
            else pending.reject(new Error('Finish or cancel earlier Explorer drags before trying again.'));
          } else if (message.event === 'candidate') this.candidate(message);
          else throw new Error('Invalid marker notification.');
        } catch (error) { clearTimeout(timeout); child.kill(); reject(error); this.onEvent({ event: 'detector-error', message: error.message }); }
      });
      child.stderr.on('data', () => {}); child.stdin.on('error', () => { this.notificationReady = false; if (!this.closed) this.onEvent({ event: 'detector-error', message: 'Explorer marker notification service stopped. Use Download to folder.' }); });
      child.on('error', error => { clearTimeout(timeout); this.startup = null; this.notificationReady = false; reject(error); });
      child.on('exit', () => { clearTimeout(timeout); this.child = null; this.startup = null; this.notificationReady = false; for (const pending of this.registrations.values()) { clearTimeout(pending.timer); pending.reject(new Error('Explorer marker notification service stopped.')); } this.registrations.clear(); if (!ready) reject(new Error('Explorer marker notification service exited.')); else if (!this.closed) this.onEvent({ event: 'detector-error', message: 'Explorer marker notification service stopped. Use Download to folder.' }); });
    });
    return this.startup;
  }
  has(id) {
    const job = this.jobs.get(id); if (!job) return false;
    return ownedPayload(job.markerPath, job.basename);
  }
  get openFolderCount() { return this.openFolders.size; }
  prepare({ name, items }) {
    if (this.closed) throw new Error('Marker detector is closed.');
    if (!this.notificationReady || !this.child?.stdin.writable) throw new Error('The Explorer marker service is starting. Keep the file selected and retry in a moment.');
    if (this.jobs.size >= 16) throw new Error('Finish or cancel the earlier Explorer drops first.');
    const id = crypto.randomUUID(), basename = `_EyeCopy_drop_${crypto.randomBytes(16).toString('hex')}.txt`;
    const markerPath = path.join(this.sourceDir, basename);
    const fd = fs.openSync(markerPath, 'wx', 0o600); try { fs.writeFileSync(fd, markerPayload(basename)); } finally { fs.closeSync(fd); }
    const job = { id, basename, markerPath, name, items, active: false, registered: false, candidates: new Set(), retry: new Map(), timer: null };
    this.jobs.set(id, job);
    // Unused prewarmed markers do not accumulate indefinitely.
    job.timer = setTimeout(() => this.cancel(id), 15 * 60 * 1000); job.timer.unref?.();
    const ready = new Promise((resolve, reject) => {
      const timer = setTimeout(() => { this.registrations.delete(basename); reject(new Error('Explorer marker registration timed out.')); }, 4000); timer.unref?.();
      this.registrations.set(basename, { id, resolve, reject, timer });
      try { this.child.stdin.write(JSON.stringify({ command: 'register-marker', basename }) + '\n'); } catch (error) { clearTimeout(timer); this.registrations.delete(basename); reject(error); }
    }).catch(error => { this.finish(id); throw error; });
    // Install a handler immediately even when the UI prewarms and releases the
    // selection before its caller waits for registration.
    ready.catch(() => {});
    return { id, markerPath, basename, ready };
  }
  begin(id) {
    const job = this.jobs.get(id); if (!job) throw new Error('Prepare this Explorer drop again.');
    if (!this.notificationReady || !job.registered) throw new Error('The Explorer marker is still preparing. Keep the file selected and drag again in a moment.');
    if (!this.has(id)) throw new Error('The temporary drag marker expired. Select the file and drag again.');
    clearTimeout(job.timer); job.active = true;
    job.timer = setTimeout(() => { this.cancel(id); this.onEvent({ event: 'timeout', id, message: 'The drop destination was not detected. Open the target local folder in Explorer and retry, or use Download to folder.' }); }, this.timeoutMs); job.timer.unref?.();
    for (const folder of job.candidates) this.check(job, folder);
  }
  candidate(message) {
    if (typeof message.basename !== 'string' || typeof message.destination !== 'string' || !/^[a-z]:\\/i.test(message.destination)) return;
    const folder = path.resolve(message.destination); if (folder.toLowerCase() === this.sourceDir.toLowerCase()) return;
    const job = [...this.jobs.values()].find(entry => entry.basename.toLowerCase() === message.basename.toLowerCase());
    if (job) {
      this.onEvent({ event: 'marker-candidate', id: job.id, basename: job.basename, destination: folder, shellEvent: Number(message.shellEvent) || 0 });
      if (job.candidates.size < 8) job.candidates.add(folder); if (job.active) this.check(job, folder);
    }
    const retired = [...this.retired.values()].find(entry => entry.basename.toLowerCase() === message.basename.toLowerCase()); if (retired) this.cleanRetired(retired, folder);
  }
  unregister(basename) { try { if (this.child?.stdin.writable) this.child.stdin.write(JSON.stringify({ command: 'unregister-marker', basename }) + '\n'); } catch { } }
  updateFolders(paths) {
    // Compatibility diagnostic only. File copies are detected exclusively from
    // the Shell's candidate notification; no directory watchers are installed.
    this.openFolders = new Set(paths.filter(value => typeof value === 'string' && /^[a-z]:\\/i.test(value)).slice(0, 64).map(value => path.resolve(value)));
  }
  async cleanupTicket(basename, folder) {
    // A manual folder choice can arrive after cancellation retired the nonce.
    // The manager retains this exact private basename; never scan the folder.
    markerPayload(basename);
    if (typeof folder !== 'string' || !/^[a-z]:\\/i.test(folder)) return false;
    for (let attempt = 0; attempt < 12; attempt++) {
      const candidate = path.join(folder, basename);
      if (!ownedPayload(candidate, basename)) return false;
      try { if (await this.claim(this.executable, folder, basename)) return true; } catch { }
      if (attempt < 11) await new Promise(resolve => setTimeout(resolve, 80));
    }
    return false;
  }
  check(job, folder) {
    if (!job.active || this.jobs.get(job.id) !== job || job.retry.has(folder)) return;
    const candidate = path.join(folder, job.basename);
    // The Shell may report create before all ticket bytes have arrived. The
    // exclusive native claim retries and verifies exact bytes before deletion.
    // A trusted CREATE notice can precede the directory entry itself. Retry
    // this exact registered nonce even while it is missing or incomplete.
    job.retry.set(folder, null);
    const attempt = async remaining => {
      let claimed = false;
      try { if (ownedPayload(candidate, job.basename)) claimed = await this.claim(this.executable, folder, job.basename); } catch { }
      if (!job.active || this.jobs.get(job.id) !== job) return;
      if (claimed) {
        job.active = false; await this.finish(job.id);
        this.onEvent({ event: 'dropped', id: job.id, destination: folder, name: job.name, items: job.items, markerConsumed: true });
      } else if (remaining > 0) { const timer = setTimeout(() => attempt(remaining - 1), 60); timer.unref?.(); job.retry.set(folder, timer); }
      else job.retry.delete(folder);
    };
    attempt(20);
  }
  async finish(id) {
    const job = this.jobs.get(id); if (!job) return;
    job.active = false; clearTimeout(job.timer); for (const timer of job.retry.values()) if (timer) clearTimeout(timer); this.jobs.delete(id);
    const pending = this.registrations.get(job.basename); if (pending) { clearTimeout(pending.timer); pending.reject(new Error('Explorer marker cancelled.')); this.registrations.delete(job.basename); }
    if (!this.retired.has(id)) this.unregister(job.basename);
    // Source cleanup uses the same exclusive handle and exact-payload check as
    // destination consumption. A changed file can never be removed blindly.
    if (path.dirname(job.markerPath) === this.sourceDir && path.basename(job.markerPath) === job.basename && ownedPayload(job.markerPath, job.basename)) {
      try { await this.claim(this.executable, this.sourceDir, job.basename); } catch { }
    }
  }
  cleanRetired(job, folder) {
    if (this.retired.get(job.id) !== job || job.cleaning.has(folder)) return;
    job.cleaning.set(folder, null);
    const attempt = async remaining => {
      let consumed = false;
      try { if (ownedPayload(path.join(folder, job.basename), job.basename)) consumed = await this.claim(this.executable, folder, job.basename); } catch { }
      if (this.retired.get(job.id) !== job) return;
      if (!consumed && remaining > 0) { const timer = setTimeout(() => attempt(remaining - 1), 60); timer.unref?.(); job.cleaning.set(folder, timer); }
      else job.cleaning.delete(folder);
    };
    attempt(20);
  }
  cancel(id) {
    const job = this.jobs.get(id);
    if (job?.active && !this.closed) {
      // A cancelled mouse gesture might already have copied its ticket. Keep
      // exact-nonce cleanup briefly; it can never trigger an original transfer.
      if (this.retired.size >= 32) { const first = this.retired.values().next().value; clearTimeout(first.expiry); for (const timer of first.cleaning.values()) if (timer) clearTimeout(timer); this.retired.delete(first.id); this.unregister(first.basename); }
      const retired = { id, basename: job.basename, cleaning: new Map(), expiry: null };
      retired.expiry = setTimeout(() => { for (const timer of retired.cleaning.values()) if (timer) clearTimeout(timer); this.retired.delete(id); this.unregister(retired.basename); }, 20000); retired.expiry.unref?.(); this.retired.set(id, retired);
      for (const folder of job.candidates) this.cleanRetired(retired, folder);
    }
    return this.finish(id);
  }
  close() { for (const id of [...this.jobs.keys()]) this.cancel(id); this.closed = true; for (const job of this.retired.values()) { clearTimeout(job.expiry); for (const timer of job.cleaning.values()) if (timer) clearTimeout(timer); } this.retired.clear(); for (const watcher of this.folders.values()) watcher.close(); this.folders.clear(); this.openFolders.clear(); if (this.child?.stdin.writable) this.child.stdin.end(); const child = this.child; if (child) setTimeout(() => { if (child.exitCode === null) child.kill(); }, 1500).unref(); }
}
module.exports = { MarkerDrop, buildMarkerProbe, claimMarker, markerPayload };
