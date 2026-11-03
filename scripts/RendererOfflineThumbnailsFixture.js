// Runs the real browsing UI against a disconnected, synthetic saved catalogue.
// Backend tests separately verify reuse of the two on-disk cache filename forms.
const { app, BrowserWindow, ipcMain, nativeImage } = require('electron');
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const assert = require('node:assert/strict');
const { pathToFileURL } = require('node:url');

const root = path.resolve(__dirname, '..');
const output = path.join(root, 'test-output', 'renderer-offline-thumbnails', crypto.randomUUID());
fs.mkdirSync(output, { recursive: true });
process.env.TEMP = output; process.env.TMP = output;
app.setPath('userData', path.join(output, 'profile'));
// Keep this harness alive while replacing the isolated renderer window.
app.on('window-all-closed', () => {});

app.whenReady().then(async () => {
  const cache = path.join(output, 'cache');
  fs.mkdirSync(cache);
  // Render real JPEGs in distinct colors; no user photographs are used.
  const imageFile = (name, rgb) => {
    const pixels = Buffer.alloc(24 * 24 * 4);
    for (let at = 0; at < pixels.length; at += 4) {
      pixels[at] = rgb[2]; pixels[at + 1] = rgb[1]; pixels[at + 2] = rgb[0]; pixels[at + 3] = 255;
    }
    const target = path.join(cache, name);
    fs.writeFileSync(target, nativeImage.createFromBitmap(pixels, { width: 24, height: 24 }).toJPEG(85));
    return pathToFileURL(target).href;
  };
  const thumbnail = imageFile('thumb-signature-a.jpg', [244, 77, 36]);
  const preview = imageFile('signature-preview-a.jpg', [65, 190, 115]);
  const videoThumbnail = imageFile('thumb-video-signature-a.jpg', [77, 145, 240]);
  const otherDevice = imageFile('signature-preview-b.jpg', [182, 89, 233]);
  const signature = { size: 1000000, modified: '2026-10-01T12:00:00Z' };
  const photo = (deviceId, index) => ({ ...signature, id: `photo-${index}`, deviceId, source: 'photos', name: `IMG_${String(index).padStart(4, '0')}.JPG`, path: `/DCIM/IMG_${index}.JPG`, kind: 'photo' });
  const a = Array.from({ length: 72 }, (_, index) => photo('offline-a', index));
  Object.assign(a[1], { name: 'CACHED_MOV.MOV', path: '/DCIM/CACHED_MOV.MOV', kind: 'video', durationSeconds: 10 });
  const b = Array.from({ length: 4 }, (_, index) => photo('offline-b', index));
  const itemsFor = id => id === 'offline-b' ? b : a;
  let selectedDevice = 'offline-a', win;
  const devices = [
    { id: 'offline-a', name: 'Saved device A', online: false, defaultDownloadDir: 'C:\\SyntheticEyeCopy\\A' },
    { id: 'offline-b', name: 'Saved device B', online: false, defaultDownloadDir: 'C:\\SyntheticEyeCopy\\B' }
  ];
  const state = () => ({ devices, selectedDevice, connection: { status: 'offline', serviceAvailable: true }, catalogue: itemsFor(selectedDevice), apps: [], jobs: [], downloadDir: 'C:\\SyntheticEyeCopy', downloadRoot: 'C:\\SyntheticEyeCopy', usb: {} });
  const forbidden = [], requests = [], errors = [];
  ipcMain.handle('eyecopy:offline-thumbnails-fixture', (event, command, args) => {
    assert.equal(event.sender, win.webContents);
    requests.push({ command, args });
    if (command === 'get_app_settings') return { stayInBackground: true, theme: 'ember' };
    if (command === 'state') return state();
    if (command === 'get_cached_catalogue') return { items: itemsFor(args.deviceId), needsLivePhotoScan: false };
    if (command === 'select_device') { selectedDevice = args.id; return state(); }
    if (command === 'thumbnail') {
      const item = args.item;
      assert.equal(item.modified, signature.modified); assert.equal(item.size, signature.size);
      if (item.deviceId === 'offline-b') return { url: otherDevice, kind: 'photo', thumbnail: true, available: true };
      if (item.id === 'photo-10') return { available: false };
      return { url: item.kind === 'video' ? videoThumbnail : Number(item.id.split('-')[1]) % 2 ? preview : thumbnail, kind: 'photo', thumbnail: true, available: true };
    }
    if (command === 'video_metadata') return { itemId: args.item.id, available: false };
    forbidden.push(command);
    throw new Error(`Offline grid must not invoke ${command}`);
  });
  const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
  const createWindow = async () => {
    win = new BrowserWindow({ show: false, width: 1480, height: 950, webPreferences: { preload: path.join(__dirname, 'RendererOfflineThumbnailsPreload.js'), contextIsolation: true, nodeIntegration: false, sandbox: false, backgroundThrottling: false } });
    win.webContents.on('console-message', (_event, level, message) => { if (level >= 3) errors.push(message); });
    await win.loadFile(path.join(root, 'ui', 'index.html'));
  };
  const js = source => win.webContents.executeJavaScript(source);
  const until = async source => {
    for (let n = 0; n < 150; n++) { const value = await js(source); if (value) return value; await wait(30); }
    throw new Error('Timed out waiting for ' + source);
  };
  const loaded = key => `(()=>{const img=document.querySelector('[data-key="${key}"] .file-preview img');return !!img&&img.complete&&img.naturalWidth===24;})()`;
  const report = { passed: false, output, checks: [] };
  const check = name => report.checks.push(name);
  try {
    await createWindow();
    await until(`document.querySelectorAll('#catalogue .file-card').length===72`);
    await until(loaded('photo-0')); await until(loaded('photo-1')); await until(loaded('photo-3'));
    assert.equal(await js(`document.querySelector('[data-key="photo-0"] .file-preview img').src`), thumbnail);
    assert.equal(await js(`document.querySelector('[data-key="photo-3"] .file-preview img').src`), preview);
    assert.equal(await js(`document.querySelector('[data-key="photo-1"] .file-preview img').src`), videoThumbnail);
    assert.equal(await js(`document.querySelectorAll('.file-card.selected').length`), 0);
    assert.equal(await js(`document.querySelector('#connection-pill').textContent.includes('offline')`), true);
    check('Disconnected startup automatically displays both cached-photo forms and cached MOV poster without selecting a file.');
    await js(`document.querySelector('[data-key="photo-10"]').scrollIntoView({block:'center'})`);
    await until(`window.bridge.fixtureCalls().some(c=>c.command==='thumbnail'&&c.args.item.id==='photo-10')`);
    await wait(100);
    assert.equal(await js(`document.querySelector('[data-key="photo-10"] .file-preview img')!==null`), false);
    check('A never-cached file keeps its placeholder and does not fetch an original.');
    await js(`document.querySelector('#catalogue-scroll').scrollTop=document.querySelector('#catalogue-scroll').scrollHeight`);
    await until(loaded('photo-71'));
    check('Scrolling automatically restores newly visible offline posters.');
    await js(`document.querySelector('#list-view-button').click();document.querySelector('#catalogue-scroll').scrollTop=0`);
    await until(loaded('photo-0'));
    assert.equal(await js(`document.querySelector('#catalogue').classList.contains('list-view')`), true);
    check('Switching to list layout retains cached images.');
    await js(`document.querySelector('[data-device="offline-b"]').click()`);
    await until(`document.querySelectorAll('#catalogue .file-card').length===4`);
    await until(loaded('photo-0'));
    assert.equal(await js(`document.querySelector('[data-key="photo-0"] .file-preview img').src`), otherDevice);
    check('Switching offline devices uses the correct device-scoped cache even with matching item IDs.');
    await js(`document.querySelector('[data-device="offline-a"]').click();document.querySelector('#grid-view-button').click();document.querySelector('#catalogue-scroll').scrollTop=0`);
    await until(`document.querySelectorAll('#catalogue .file-card').length===72`); await until(loaded('photo-0'));
    assert.equal(await js(`document.querySelector('[data-key="photo-0"] .file-preview img').src`), thumbnail);
    check('Returning to an offline device retains its own cached images.');
    win.destroy(); await createWindow();
    await until(`document.querySelectorAll('#catalogue .file-card').length===72`);
    await js(`document.querySelector('#catalogue-scroll').scrollTop=0`);
    await until(loaded('photo-0')); await until(loaded('photo-3'));
    check('A fresh renderer process page restores offline cached previews automatically again.');
    assert.deepEqual(forbidden, []);
    assert.equal(requests.some(call => ['preview','download','stream','stream_file','scan','upload'].includes(call.command)), false);
    assert.equal(await js(`document.querySelectorAll('.file-card.selected').length`), 0);
    assert.deepEqual(errors, []);
    check('No preview action, scan, original transfer, playback, selection, or console error is needed to populate the grid.');
    Object.assign(report, { passed: true, thumbnailRequests: requests.filter(call=>call.command==='thumbnail').length, consoleErrors: errors, forbiddenCommands: forbidden });
  } catch (error) { report.error = error.stack; report.consoleErrors = errors; report.forbiddenCommands = forbidden; process.exitCode = 1; }
  finally {
    fs.writeFileSync(path.join(output, 'report.json'), JSON.stringify(report, null, 2));
    fs.writeFileSync(path.join(root, 'test-output', 'renderer-offline-thumbnails.json'), JSON.stringify(report, null, 2));
    console.log(JSON.stringify(report, null, 2));
    win?.destroy(); app.exit(report.passed ? 0 : 1);
  }
});
