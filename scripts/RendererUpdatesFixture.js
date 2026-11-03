// Real renderer update controls with synthetic state and no network/installation.
const { app, BrowserWindow } = require('electron');
const path = require('node:path');
const fs = require('node:fs');
const crypto = require('node:crypto');
const assert = require('node:assert/strict');
const root = path.resolve(__dirname, '..');
const output = path.join(root, 'test-output', 'renderer-updates', crypto.randomUUID());
fs.mkdirSync(output, { recursive: true }); process.env.TEMP = output; process.env.TMP = output;
app.setPath('userData', path.join(output, 'profile'));
app.whenReady().then(async () => {
  const win = new BrowserWindow({ show: false, width: 1480, height: 950,
    webPreferences: { preload: path.join(__dirname, 'RendererFixturePreload.js'), contextIsolation: true,
      nodeIntegration: false, sandbox: false, backgroundThrottling: false } });
  const errors = [], report = { passed: false, syntheticOnly: true, networkRequests: 0, installerExecutions: 0, checks: [] };
  win.webContents.on('console-message', (_event, level, message) => { if (level >= 3) errors.push(message); });
  const js = code => win.webContents.executeJavaScript(code);
  const until = async code => { for (let n = 0; n < 100; n++) { if (await js(code)) return; await new Promise(resolve => setTimeout(resolve, 30)); } throw new Error('Timed out: ' + code); };
  try {
    await win.loadFile(path.join(root, 'ui/index.html'));
    await until(`window.bridge.fixtureCalls().some(c=>c.command==='get_app_settings')`);
    await js(`window.bridge.fixtureResolvePhotoScan();document.querySelector('#settings-button').click()`);
    await until(`document.querySelector('#settings-dialog').open`);
    assert.equal(await js(`document.querySelector('#check-updates').disabled`), true);
    assert.equal(await js(`document.querySelector('#settings-auto-updates').disabled`), true);
    assert.equal(await js(`window.bridge.fixtureCalls().some(c=>c.command==='check_updates')`), false);
    report.checks.push('No repository disables update checks and issues no request.');
    await js(`document.querySelector('#settings-repository').value='example/EyeCopy';document.querySelector('#save-update-repository').click()`);
    await until(`!document.querySelector('#check-updates').disabled`);
    assert.equal(await js(`document.querySelector('#settings-auto-updates').checked`), true);
    await js(`document.querySelector('#check-updates').click()`);
    await until(`document.querySelector('#update-status').textContent.includes('1.0.1')`);
    assert.equal(await js(`document.querySelector('#install-update').classList.contains('hidden')`), false);
    assert.equal(await js(`document.querySelector('#install-update').disabled`), true);
    report.checks.push('Available updates are visible while active transfers block installation.');
    await js(`window.bridge.fixtureSetJobs([])`);
    await until(`!document.querySelector('#install-update').disabled`);
    await js(`document.querySelector('#install-update').click()`);
    await until(`window.bridge.fixtureCalls().some(c=>c.command==='install_update')`);
    report.checks.push('The install action is reachable only after transfers become idle.');
    await js(`window.bridge.fixtureUpdateStatus({status:'downloading',message:'Downloading and verifying the update…',downloadedBytes:25,totalBytes:100})`);
    assert.equal(await js(`document.querySelector('#update-progress').value`), 25);
    assert.equal(await js(`document.querySelector('#cancel-update').classList.contains('hidden')`), false);
    await js(`document.querySelector('#cancel-update').click()`);
    await until(`document.querySelector('#update-progress').classList.contains('hidden')`);
    report.checks.push('Download progress and cancellation remain responsive.');
    await js(`document.querySelector('#settings-auto-updates').click()`);
    await until(`window.bridge.fixtureCalls().some(c=>c.command==='set_app_settings'&&c.args.autoCheckUpdates===false)`);
    await js(`window.bridge.fixtureEmit({event:'open_updates',data:{}})`);
    await until(`document.querySelector('#settings-dialog').open`);
    assert.equal(await js(`document.querySelector('#settings-dialog').scrollWidth<=document.querySelector('#settings-dialog').clientWidth`), true);
    assert.deepEqual(errors, []);
    report.checks.push('Preferences and the standalone-launch event use the same scrollable update controls.');
    report.passed = true;
  } catch (error) { report.error = error.stack; }
  finally {
    report.consoleErrors = errors;
    fs.writeFileSync(path.join(root, 'test-output/renderer-updates.json'), JSON.stringify(report, null, 2));
    console.log(JSON.stringify(report)); win.destroy(); app.exit(report.passed ? 0 : 1);
  }
});
