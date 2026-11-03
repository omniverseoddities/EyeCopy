const { app, BrowserWindow, ipcMain, dialog, shell, protocol, net, Tray, Menu, nativeImage } = require('electron');
const { spawn } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const readline = require('node:readline');
const { pathToFileURL } = require('node:url');
const crypto = require('node:crypto');
const { controlQueue } = require('./queue-control');
const { createQueueOrder } = require('./queue-order');
const { installMediaPermissions } = require('./media-permissions');
const { createAppSettings, shouldHideOnClose } = require('./app-settings');
const { registerProfile } = require('./profile-ownership');
const { createUpdateService } = require('./update-service');
const { createUpdateInstallController, requireUpdateOperationAllowed, UPDATE_MUTATIONS } = require('./update-install');
const queueOrder = createQueueOrder();

app.setName('EyeCopy');
app.setAppUserModelId('com.eyecopy.desktop');
protocol.registerSchemesAsPrivileged([{ scheme: 'eyecopy-media', privileges: { standard: true, secure: true, supportFetchAPI: true, stream: true } }]);
const root = path.resolve(__dirname, '..');
const uiURL = pathToFileURL(path.join(root, 'ui', 'index.html')).href;
const profileBase = process.env.LOCALAPPDATA || path.join(app.getPath('home'), 'AppData', 'Local');
// Store user data separately from the application files.
const defaultProfile = path.join(profileBase, 'EyeCopy');
const dataDir = process.env.EYECOPY_DATA_DIR || (!app.isPackaged ? path.join(root, 'data') : defaultProfile);
app.setPath('userData', path.join(dataDir, 'desktop'));
let win, worker, tray, shuttingDown = false, sequence = 0, restartCount = 0, usbRefreshBusy = false, usbRefreshPending = false, usbDeviceSignature = '', seenDeviceState = false;
let dragManager, backendJobs = [];
const pending = new Map(), mediaFiles = new Map();
const backendCommands = new Set(['state', 'get_cached_catalogue', 'thumbnail', 'video_metadata', 'select_device', 'rename_device', 'scan', 'list_apps', 'download', 'upload', 'cancel_job', 'retry_job', 'pause_job', 'clear_cache', 'set_download_dir', 'reset_download_dir', 'set_default_download_dir', 'undo_download_location', 'redo_download_location', 'preview_phone_folder_delete', 'confirm_phone_folder_delete', 'undo_pc_cleanup', 'redo_pc_cleanup', 'get_pc_cleanup_history', 'pair', 'preview', 'stream', 'stream_file', 'stop_stream', 'resolve_transfer_items', 'files_home', 'configure_files_home', 'ensure_files_home', 'create_folder', 'preview_phone_delete', 'confirm_phone_delete', 'preview_pc_cleanup', 'confirm_pc_cleanup']);
fs.mkdirSync(dataDir, { recursive: true });
try {
  const ownership = registerProfile(dataDir, {
    applicationRoot: app.isPackaged ? path.dirname(app.getPath('exe')) : root,
    protectedRoots: ['home', 'desktop', 'documents', 'downloads', 'pictures', 'videos'].map(name => app.getPath(name))
  });
  if (!ownership.registered) fs.appendFileSync(path.join(dataDir, 'desktop.log'), `Uninstall profile registration: ${ownership.reason}\n`);
} catch (error) {
  fs.appendFileSync(path.join(dataDir, 'desktop.log'), `Uninstall profile registration could not complete: ${error.message}\n`);
}
const packageMetadata = JSON.parse(fs.readFileSync(path.join(root, 'package.json'), 'utf8'));
const packageRepository = typeof packageMetadata.repository === 'string' ? packageMetadata.repository : packageMetadata.repository?.url || '';
const appSettings = createAppSettings(dataDir, { defaultRepository: packageRepository.replace(/^git\+/, '') });
const logPath = path.join(dataDir, 'desktop.log');
const log = message => fs.appendFileSync(logPath, `${new Date().toISOString()} ${message}\n`);
log(`EyeCopy started: desktop PID ${process.pid}.`);
const updates = createUpdateService({ version: packageMetadata.version, dataDir,
  repository: appSettings.get().updateRepository, fetch: (url, options) => net.fetch(url, options),
  onChange: status => sendEvent('update_status', status) });
let automaticUpdateTimer, pendingUpdateSetup = null;
async function requireIdleForUpdate() {
  const busy = () => [...pending.values()].some(entry => UPDATE_MUTATIONS.has(entry.command)) ||
    Boolean(dragManager?.inflight?.size || dragManager?.pendingCancellations?.size);
  if (busy()) throw new Error('Finish or stop transfers and file operations before installing an update.');
  const snapshot = await request('state');
  const jobs = [...(snapshot.jobs || []), ...(dragManager?.jobs() || [])];
  if (busy() || jobs.some(job => ['queued', 'transferring', 'paused', 'waiting', 'waiting_for_approval'].includes(job.state))) {
    throw new Error('Finish or stop every transfer before installing an update.');
  }
}
function openUpdates() { showWindow(); sendEvent('open_updates', updates.get()); }
const updateInstaller = createUpdateInstallController({
  packaged: () => app.isPackaged,
  service: updates,
  requireIdle: requireIdleForUpdate,
  confirm: async candidate => (await dialog.showMessageBox(win, { type: 'question', title: 'Install EyeCopy update',
    message: `Install EyeCopy ${candidate.version}?`,
    detail: `The verified Windows installer comes from ${candidate.repository}. EyeCopy will close. Your settings and downloaded originals will be kept.`,
    buttons: ['Install update', 'Cancel'], defaultId: 1, cancelId: 1, noLink: true })).response === 0,
  ready: verified => { pendingUpdateSetup = verified.path; setTimeout(() => app.quit(), 0); }
});
function sendEvent(event, data) {
  if (event === 'state') dragManager?.observeState(data);
  if (event === 'catalogue' && Array.isArray(data?.items)) dragManager?.observeState({ catalogue: data.items });
  if (event === 'jobs') { backendJobs = data; data = queueOrder([...data, ...(dragManager?.jobs() || [])]); }
  if (event === 'state' && data?.jobs) { backendJobs = data.jobs; data = { ...data, jobs: queueOrder([...data.jobs, ...(dragManager?.jobs() || [])]) }; }
  if (win && !win.isDestroyed()) win.webContents.send('bridge:event', { event, data });
}
async function openLocalPath(value) {
  if (typeof value !== 'string' || !path.isAbsolute(value)) throw new Error('Choose an existing local file or folder.');
  const target = path.resolve(value);
  let stat;
  try { stat = await fs.promises.stat(target); }
  catch (error) {
    if (error.code !== 'ENOENT') throw error;
    const state = await request('state');
    const known = (state.devices || []).some(device => device.defaultDownloadDir && path.resolve(device.defaultDownloadDir).toLowerCase() === target.toLowerCase());
    if (!known) throw new Error('That local file or folder is no longer available.');
    await fs.promises.mkdir(target, { recursive: true });
    stat = await fs.promises.stat(target);
  }
  if (stat.isDirectory()) { const error = await shell.openPath(target); if (error) throw new Error(error); }
  else shell.showItemInFolder(target);
  return true;
}
function grantMedia(result) {
  if (!result) return result;
  let source;
  if (result.url) {
    const value = new URL(result.url);
    if (value.protocol !== 'http:' || value.hostname !== '127.0.0.1' || !value.port || !/^\/[A-Za-z0-9_-]{20,128}\/(original|compatible)$/.test(value.pathname) || value.search || value.username || value.password) throw new Error('The media service returned an invalid stream.');
    source = { remote: result.url };
  } else if (result.path) {
    const localPath = path.resolve(result.path);
    if (!fs.existsSync(localPath)) throw new Error('Preview file is unavailable.');
    source = { file: localPath };
  } else return result;
  const token = crypto.randomUUID();
  mediaFiles.set(token, source);
  if (mediaFiles.size > 10000) mediaFiles.delete(mediaFiles.keys().next().value);
  return { ...result, url: `eyecopy-media://${token}/${encodeURIComponent(result.name || (result.path ? path.basename(result.path) : 'stream.mp4'))}` };
}
async function openGuide(filename, title) {
  const guideURL = pathToFileURL(path.join(root, filename)).href;
  const localGuides = new Set(['GUIDE.html', 'Transport Options.html'].map(name => pathToFileURL(path.join(root, name)).href));
  const guide = new BrowserWindow({ parent: win, width: 980, height: 880, title, autoHideMenuBar: true, backgroundColor: '#0b090b', webPreferences: { nodeIntegration: false, contextIsolation: true, sandbox: true } });
  guide.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
  guide.webContents.on('will-navigate', (event, url) => {
    if (!localGuides.has(url)) { event.preventDefault(); if (/^https:\/\/(support\.apple\.com|developer\.apple\.com|apps\.microsoft\.com|learn\.microsoft\.com|github\.com|libimobiledevice\.org|apps\.apple\.com|docs\.videolan\.me)\//.test(url)) shell.openExternal(url); }
  });
  await guide.loadFile(path.join(root, filename));
}

function request(command, args = {}) {
  if (!worker || worker.killed || !worker.stdin.writable) return Promise.reject(new Error('Transfer engine is starting. Please try again in a moment.'));
  const id = ++sequence;
  return new Promise((resolve, reject) => {
    const timeout = command === 'pair' ? 45000 : command === 'confirm_phone_folder_delete' ? 180000 : command === 'preview_phone_folder_delete' ? 90000 : 60000;
    const timer = setTimeout(() => { pending.delete(id); reject(new Error('The operation did not respond in time. The interface remains available; refresh to check its result before trying again.')); }, timeout);
    pending.set(id, { resolve, reject, timer, command });
    worker.stdin.write(JSON.stringify({ id, command, args }) + '\n', error => {
      if (error) { clearTimeout(timer); pending.delete(id); reject(error); }
    });
  });
}
function startWorker() {
  const frozenEngine = path.join(root, 'runtime', 'engine', 'EyeCopyEngine.exe');
  const python = path.join(root, '.venv', 'Scripts', 'python.exe');
  const executable = fs.existsSync(frozenEngine) ? frozenEngine : python;
  if (!fs.existsSync(executable)) {
    dialog.showErrorBox('EyeCopy runtime', 'The transfer engine is missing. Extract the complete EyeCopy Windows package again.');
    return;
  }
  const workerArgs = [ ...(executable === python ? ['-u', path.join(root, 'backend', 'engine.py')] : []), '--data-dir', dataDir, '--download-dir', path.join(app.getPath('videos'), 'EyeCopy') ];
  worker = spawn(executable, workerArgs, {
    cwd: root, windowsHide: true, stdio: ['pipe', 'pipe', 'pipe'], env: { ...process.env, PYTHONUTF8: '1', PYTHONUNBUFFERED: '1', PATH: path.join(root, 'vendor', 'ffmpeg') + path.delimiter + (process.env.PATH || '') }
  });
  readline.createInterface({ input: worker.stdout }).on('line', line => {
    try {
      const message = JSON.parse(line);
      if (message.event) {
        sendEvent(message.event, message.data);
        if (message.event === 'state' && Array.isArray(message.data?.devices)) {
          const signature = message.data.devices.filter(device => device.online).map(device => device.id).sort().join('|');
          const connectedBefore = new Set(usbDeviceSignature.split('|').filter(Boolean));
          if (seenDeviceState && signature.split('|').some(id => id && !connectedBefore.has(id))) showWindow();
          seenDeviceState = true;
          if (signature !== usbDeviceSignature) {
            usbDeviceSignature = signature;
            if (!usbRefreshBusy) diagnostics().catch(error => { if (!shuttingDown) log(`USB refresh: ${error.message}`); });
            else usbRefreshPending = true;
          }
        }
      }
      else if (pending.has(message.id)) {
        const entry = pending.get(message.id); pending.delete(message.id); clearTimeout(entry.timer);
        if (message.error) entry.reject(new Error(typeof message.error === 'string' ? message.error : message.error.message || 'Transfer engine error'));
        else entry.resolve(message.result);
      }
    } catch { log('Ignored malformed transfer-engine message.'); }
  });
  worker.stderr.on('data', chunk => log(String(chunk).slice(0, 8000)));
  worker.on('error', error => log(`Engine failed to start: ${error.message}`));
  worker.on('exit', code => {
    worker = null;
    for (const entry of pending.values()) { clearTimeout(entry.timer); entry.reject(new Error('Transfer engine restarted. Cached files and partial downloads are retained.')); }
    pending.clear();
    if (!shuttingDown) {
      log(`Engine exited: ${code}`);
      sendEvent('connection', { status: 'offline', serviceAvailable: false, message: 'Transfer engine stopped. Restarting; cached files are retained.' });
      if (restartCount++ < 3) setTimeout(startWorker, 1500);
    }
  });
}
function validateSender(event) {
  if (!win || event.sender !== win.webContents || event.senderFrame?.url !== uiURL) throw new Error('Unrecognized interface.');
}
async function diagnostics() {
  if (usbRefreshBusy) throw new Error('USB diagnostics are already running. The result will appear shortly.');
  usbRefreshBusy = true;
  const reportPath = path.join(dataDir, 'usb-diagnostics.json');
  try { await new Promise((resolve, reject) => {
    const probe = spawn('powershell.exe', ['-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', path.join(root, 'diagnostics', 'Get-IPhoneUsbDiagnostics.ps1'), '-OutputPath', reportPath], { windowsHide: true, stdio: ['ignore', 'ignore', 'pipe'] });
    let errorText = '';
    const timer = setTimeout(() => { probe.kill(); reject(new Error('USB diagnostics timed out. Cached connection details remain available.')); }, 40000);
    probe.stderr.on('data', data => { errorText += data; });
    probe.on('error', error => { clearTimeout(timer); reject(error); });
    probe.on('exit', code => { clearTimeout(timer); if (code === 0) resolve(); else reject(new Error(errorText.slice(0, 300) || 'Windows could not query the USB hub.')); });
  });
  const report = JSON.parse(fs.readFileSync(reportPath, 'utf8').replace(/^\uFEFF/, ''));
  sendEvent('usb', report);
  if (!shuttingDown && worker) {
    const currentState = await request('state');
    sendEvent('state', currentState);
  }
  return report;
  } finally {
    usbRefreshBusy = false;
    if (usbRefreshPending && !shuttingDown) {
      usbRefreshPending = false;
      diagnostics().catch(error => { if (!shuttingDown) log(`USB refresh: ${error.message}`); });
    }
  }
}
ipcMain.handle('bridge:request', async (event, command, args = {}) => {
  validateSender(event);
  if (typeof command !== 'string' || !args || typeof args !== 'object') throw new Error('Invalid request.');
  if (shuttingDown) throw new Error('EyeCopy is closing.');
  requireUpdateOperationAllowed(command, updateInstaller.busy() || Boolean(pendingUpdateSetup));
  if (command === 'get_app_settings') return appSettings.get();
  if (command === 'get_update_status') return updates.get();
  if (command === 'check_updates') return updates.check();
  if (command === 'cancel_update') return updates.cancel();
  if (command === 'install_update') return updateInstaller.install();
  if (command === 'set_app_settings') {
    if (pendingUpdateSetup) throw new Error('EyeCopy is closing to install the update.');
    const settings = appSettings.update(args);
    updates.configure(settings.updateRepository);
    if (!settings.autoCheckUpdates) updates.cancelAutomatic();
    sendEvent('app_settings', settings);
    if (settings.autoCheckUpdates && settings.updateRepository) updates.check({ automatic: true }).catch(error => log(`Update check: ${error.message}`));
    return settings;
  }
  if (command === 'prepare_drag') {
    const prepared = await dragManager.prepare(args);
    return { ready: true, count: prepared.items.length, virtual: Boolean(prepared.files), marker: Boolean(prepared.marker), mode: prepared.marker ? 'marker' : prepared.localPaths ? 'local' : 'virtual' };
  }
  if (['cancel_job', 'retry_job', 'pause_job'].includes(command) && dragManager?.control(args.id, command)) return { changed: true };
  if (['cancel_all_jobs', 'clear_all_jobs'].includes(command)) return controlQueue(command, {
    request, manager: dragManager, getBackendJobs: () => backendJobs,
    setBackendJobs: jobs => { backendJobs = jobs; }, log
  }).then(result => ({ ...result, jobs: queueOrder(result.jobs) }));
  if (command === 'clear_finished_jobs') {
    const result = await request('clear_finished_jobs');
    dragManager?.clearFinished();
    return { ...result, jobs: queueOrder([...result.jobs, ...(dragManager?.jobs() || [])]) };
  }
  if (command === 'choose_job_folder') {
    const selection = dragManager?.selection(args.id);
    if (!selection) throw new Error('Select a pending device drop from the queue.');
    const result = await dialog.showOpenDialog(win, { title: 'Choose destination for queued device files', properties: ['openDirectory', 'createDirectory'] });
    if (!result.canceled) await dragManager.setDestination(args.id, result.filePaths[0]);
    return { jobs: queueOrder([...backendJobs, ...dragManager.jobs()]) };
  }
  if (command === 'stop_stream' && typeof args.url === 'string' && args.url.startsWith('eyecopy-media:')) {
    const token = new URL(args.url).hostname, source = mediaFiles.get(token);
    mediaFiles.delete(token);
    return source?.remote ? request('stop_stream', { url: source.remote }) : { stopped: true };
  }
  if (command === 'pick_folder') {
    const result = await dialog.showOpenDialog(win, { title: 'Choose download folder', properties: ['openDirectory', 'createDirectory'] });
    return result.canceled ? null : result.filePaths[0];
  }
  if (command === 'pick_upload_files') {
    const result = await dialog.showOpenDialog(win, {
      title: 'Upload to Files', properties: ['openFile', 'multiSelections']
    });
    return result.canceled ? [] : result.filePaths;
  }
  if (command === 'open_path') {
    return openLocalPath(args.path);
  }
  if (command === 'show_guide') {
    await openGuide('GUIDE.html', 'EyeCopy · Setup and use'); return true;
  }
  if (command === 'diagnostics') return diagnostics();
  if (['preview_pc_delete', 'confirm_pc_delete'].includes(command)) return request(command, args);
  if (!backendCommands.has(command)) throw new Error('Unknown action.');
  const result = await request(command, args);
  if (['preview', 'thumbnail', 'stream'].includes(command)) return grantMedia(result);
  if (command === 'state' && result?.jobs) { dragManager?.observeState(result); backendJobs = result.jobs; return { ...result, jobs: queueOrder([...result.jobs, ...(dragManager?.jobs() || [])]) }; }
  return result;
});
ipcMain.on('bridge:drag', async (event, payload) => {
  try {
    validateSender(event);
    requireUpdateOperationAllowed('native_drag', updateInstaller.busy() || Boolean(pendingUpdateSetup));
    await dragManager.start(payload, event.sender);
  } catch (error) { log(`Drag unavailable: ${error.message}`); sendEvent('error', { message: error.message }); }
});

function showWindow() {
  if (!win || win.isDestroyed()) return;
  if (win.isMinimized()) win.restore();
  win.show(); win.focus();
}
function createTray() {
  tray = new Tray(nativeImage.createFromPath(path.join(root, 'assets', 'eyecopy.png')));
  tray.setToolTip('EyeCopy · Device files');
  tray.setContextMenu(Menu.buildFromTemplate([
    { label: 'Open EyeCopy', click: showWindow },
    { label: 'Check for updates', click: openUpdates },
    { label: 'Open download folder', click: async () => { try { const state = await request('state'); await openLocalPath(state.downloadDir); } catch (error) { log(error.message); } } },
    { type: 'separator' },
    { label: 'Quit EyeCopy', click: () => app.quit() }
  ]));
  tray.on('click', showWindow);
  tray.on('double-click', showWindow);
}
if (!app.requestSingleInstanceLock()) app.quit();
else {
  app.on('second-instance', (_event, argv) => { if (argv.includes('--quit')) app.quit(); else if (argv.includes('--updates')) openUpdates(); else showWindow(); });
  app.whenReady().then(async () => {
    protocol.handle('eyecopy-media', incoming => {
      const source = mediaFiles.get(new URL(incoming.url).hostname);
      if (!source) return new Response('Preview unavailable', { status: 404 });
      return net.fetch(source.remote || pathToFileURL(source.file).href, { method: incoming.method, headers: incoming.headers, signal: incoming.signal });
    });
    const { DragManager } = require('./drag-manager');
    dragManager = new DragManager({ root, dataDir, request, log, emit: () => { if (win && !win.isDestroyed()) win.webContents.send('bridge:event', { event: 'jobs', data: queueOrder([...backendJobs, ...dragManager.jobs()]) }); } });
    win = new BrowserWindow({ width: 1480, height: 950, minWidth: 1080, minHeight: 700, backgroundColor: '#0b090b', title: 'EyeCopy', icon: path.join(root, 'assets', 'eyecopy.ico'), autoHideMenuBar: true, show: false,
      webPreferences: { preload: path.join(__dirname, 'preload.js'), contextIsolation: true, nodeIntegration: false, sandbox: true, webSecurity: true } });
    installMediaPermissions(win, uiURL);
    win.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
    win.webContents.on('will-navigate', (event, url) => { if (url !== uiURL) event.preventDefault(); });
    win.webContents.on('render-process-gone', (_event, details) => log(`Renderer stopped: ${details.reason}`));
    win.webContents.on('console-message', (_event, level, message) => { if (level >= 2) log(`Renderer: ${message}`); });
    win.on('close', event => {
      if (shuttingDown) return;
      event.preventDefault();
      if (shouldHideOnClose(appSettings.get(), shuttingDown, Boolean(tray && !tray.isDestroyed()))) win.hide();
      else app.quit();
    });
    createTray();
    Menu.setApplicationMenu(Menu.buildFromTemplate([{ label: 'EyeCopy', submenu: [
      { label: 'Open EyeCopy', click: showWindow },
      { label: 'Check for updates', click: openUpdates },
      { label: 'Quit EyeCopy', accelerator: 'Control+Shift+Q', click: () => app.quit() }
    ] }]));
    startWorker();
    await win.loadFile(path.join(root, 'ui', 'index.html'));
    win.show();
    if (process.argv.includes('--updates')) openUpdates();
    const automaticCheck = () => { const settings = appSettings.get(); if (settings.autoCheckUpdates && settings.updateRepository) updates.check({ automatic: true }).catch(error => log(`Update check: ${error.message}`)); };
    setTimeout(automaticCheck, 3000).unref();
    automaticUpdateTimer = setInterval(automaticCheck, 6 * 3600 * 1000); automaticUpdateTimer.unref();
    // Launch the warm gesture helper after its Electron parent is visible.
    // Windows foreground rules use the launching process for input handoff.
    dragManager.startHelper();
  }).catch(error => { dialog.showErrorBox('EyeCopy could not open', error.message); app.quit(); });
}
app.on('before-quit', event => {
  if (shuttingDown) return;
  shuttingDown = true; dragManager?.close();
  clearInterval(automaticUpdateTimer); updates.dispose();
  tray?.destroy();
  if (worker && !worker.killed) {
    // EOF runs the engine's cleanup, closing phone streams and FFmpeg before
    // the process exits. Retain a bounded fallback for a hung phone request.
    event.preventDefault();
    const closingWorker = worker;
    closingWorker.once('exit', () => app.quit());
    closingWorker.stdin.end();
    setTimeout(() => { if (closingWorker.exitCode === null) closingWorker.kill(); app.quit(); }, 3000).unref();
  }
});
app.on('window-all-closed', () => { if (shuttingDown) app.quit(); });
app.on('will-quit', () => {
  if (!pendingUpdateSetup) return;
  const target = path.dirname(app.getPath('exe'));
  if (/["\r\n\0]/.test(target)) { log('The update installer needs a normal local application folder.'); return; }
  try {
    const installer = spawn(pendingUpdateSetup, ['/D=' + target], {
      detached: true, stdio: 'ignore', windowsHide: false, windowsVerbatimArguments: true, shell: false
    });
    installer.on('error', error => log(`Update installer could not open: ${error.message}`)); installer.unref();
  } catch (error) { log(`Update installer could not open: ${error.message}`); }
});
