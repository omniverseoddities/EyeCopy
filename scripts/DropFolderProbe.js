'use strict';
// Read-only diagnostic/confirmation fallback. A verified directory is not
// proof that Explorer accepted a native drop; never auto-transfer on that alone.
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const { spawn } = require('node:child_process');
async function buildDropFolderProbe(dataDir) {
  if (process.platform !== 'win32') throw new Error('Explorer target checks require Windows.');
  const bundled = path.join(__dirname, '..', 'assets', 'native', 'DropFolderProbe.exe');
  if (fs.existsSync(bundled)) return bundled;
  const source = path.join(__dirname, 'DropFolderProbe.cs');
  const hash = crypto.createHash('sha256').update(await fs.promises.readFile(source)).digest('hex').slice(0, 20);
  const output = path.resolve(dataDir, 'drop-folder-probe', hash), executable = path.join(output, 'DropFolderProbe.exe');
  if (fs.existsSync(executable)) return executable;
  const framework = path.join(process.env.SystemRoot || 'C:\\Windows', 'Microsoft.NET', 'Framework64', 'v4.0.30319');
  const compiler = path.join(framework, 'csc.exe');
  const references = ['UIAutomationClient.dll', 'UIAutomationTypes.dll', 'WindowsBase.dll'].map(name => path.join(framework, 'WPF', name));
  if (![compiler, ...references].every(file => fs.existsSync(file))) throw new Error('Windows read-only UI Automation dependencies are unavailable.');
  await fs.promises.mkdir(output, { recursive: true });
  await new Promise((resolve, reject) => {
    const args = ['/nologo', '/target:exe', '/platform:x64', '/optimize+', '/reference:Microsoft.CSharp.dll', '/reference:System.Web.Extensions.dll'].concat(references.map(file => '/reference:' + file), ['/out:' + executable, source]);
    const child = spawn(compiler, args, { windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'] });
    let diagnostic = '';
    child.stdout.on('data', bytes => { diagnostic += String(bytes).slice(0, 6000); });
    child.stderr.on('data', bytes => { diagnostic += String(bytes).slice(0, 6000); });
    child.on('error', reject);
    child.on('exit', code => code === 0 ? resolve() : reject(new Error('Explorer read-only probe could not compile: ' + diagnostic.slice(0, 1500))));
  });
  return executable;
}
async function runDropFolderProbe(executable, args, timeoutMs = 3500) {
  return new Promise((resolve, reject) => {
    const child = spawn(executable, args, { windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'] });
    let output = '', settled = false;
    const settle = (error, result) => { if (settled) return; settled = true; clearTimeout(timer); error ? reject(error) : resolve(result); };
    const timer = setTimeout(() => { child.kill(); settle(null, { verified: false, reason: 'Explorer did not respond to the read-only target check.' }); }, timeoutMs);
    child.stdout.on('data', chunk => { output += String(chunk); if (output.length > 1024 * 1024) { child.kill(); settle(new Error('Explorer probe returned too much data.')); } });
    child.stderr.on('data', () => {});
    child.on('error', error => settle(error));
    child.on('exit', () => { if (settled) return; try { settle(null, JSON.parse(output.trim())); } catch { settle(new Error('Explorer probe returned an invalid response.')); } });
  });
}
async function resolveDropFolder(executable, { x, y }, { timeoutMs = 3500 } = {}) {
  if (![x, y].every(value => Number.isInteger(value) && value >= -2147483648 && value <= 2147483647)) throw new Error('Use a fixed physical pixel point for the Explorer target check.');
  const result = await runDropFolderProbe(executable, ['--point', String(x), String(y)], timeoutMs);
  if (!result || typeof result !== 'object' || typeof result.verified !== 'boolean') throw new Error('Explorer probe returned invalid target metadata.');
  if (result.verified) {
    if (typeof result.directory !== 'string' || !/^[a-z]:\\/i.test(result.directory) || !path.isAbsolute(result.directory)
        || !/^\d{1,20}$/.test(result.rootWindow) || !/^\d{1,20}$/.test(result.viewWindow) || !Number.isInteger(result.processId)
        || result.addressSource !== 'COM active visible Shell view' || result.coordinateSpace !== 'physical pixels'
        || result.readOnly !== true || result.dropAccepted !== false || result.markerConsumed !== false) throw new Error('Explorer probe returned unverified target metadata.');
  }
  return result;
}
async function inventoryDropFolders(executable, { timeoutMs = 10000 } = {}) { return runDropFolderProbe(executable, ['--inventory'], timeoutMs); }
module.exports = { buildDropFolderProbe, resolveDropFolder, inventoryDropFolders };
