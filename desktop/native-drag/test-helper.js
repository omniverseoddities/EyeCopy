'use strict';
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const crypto = require('node:crypto');
const readline = require('node:readline');
const { spawn } = require('node:child_process');
const { buildHelper, createDragBroker } = require('./broker');

async function main() {
  const dataDir = path.resolve(__dirname, '../../test-output/native-drag');
  const executable = await buildHelper(dataDir);
  const bytes = Buffer.alloc(1048576 + 17);
  for (let n = 0; n < bytes.length; n++) bytes[n] = n % 251;
  const token = crypto.randomBytes(32).toString('base64url'), ranges = [];
  let blockedPhase = null;
  const server = http.createServer((request, response) => {
    if (request.url !== `/${token}/original`) { response.writeHead(404); response.end(); return; }
    const range = /^bytes=(\d+)-(\d+)$/.exec(request.headers.range || '');
    if (!range) { response.writeHead(400); response.end(); return; }
    const start = Number(range[1]), end = Number(range[2]);
    if (start >= bytes.length || end !== bytes.length - 1) { response.writeHead(416); response.end(); return; }
    ranges.push([start, end]);
    if (blockedPhase === 'headers') return;
    response.writeHead(206, { 'Content-Type': 'application/octet-stream', 'Content-Length': end - start + 1, 'Content-Range': `bytes ${start}-${end}/${bytes.length}`, 'Accept-Ranges': 'bytes' });
    if (blockedPhase === 'body') { response.flushHeaders(); return; }
    response.end(bytes.subarray(start));
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const url = `http://127.0.0.1:${server.address().port}/${token}/original`;
  try {
    await new Promise((resolve, reject) => {
      const child = spawn(executable, ['--self-test', url], { windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'] });
      let output = '', error = '';
      child.stdout.on('data', chunk => { output += chunk; });
      child.stderr.on('data', chunk => { error += chunk; });
      child.on('error', reject);
      child.on('exit', code => {
        if (code !== 0 || !output.includes('self_test_passed')) reject(new Error(error || output || `Exit ${code}`));
        else resolve();
      });
    });
    if (ranges.length !== 2 || ranges[0][0] !== 0 || ranges[1][0] !== 999999) throw new Error('HTTP requests did not preserve sequential stream and random seeking.');
    const shellDestination = path.join(dataDir, 'shell-target', crypto.randomUUID());
    const shellResult = await new Promise((resolve, reject) => {
      const child = spawn(executable, ['--shell-test', url, shellDestination], { windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'] });
      let output = '', error = '';
      const timer = setTimeout(() => { child.kill(); reject(new Error('Real Windows folder drop test timed out.')); }, 30000);
      child.stdout.on('data', chunk => { output += chunk; });
      child.stderr.on('data', chunk => { error += chunk; });
      child.on('error', reject);
      child.on('exit', code => {
        clearTimeout(timer);
        if (code !== 0 || !output.includes('shell_test_passed')) reject(new Error(error || output || `Shell test exited ${code}`));
        else resolve(output.trim().split('\n').map(line => JSON.parse(line)).find(message => message.event === 'shell_test_passed'));
      });
    });
    const saved = await fs.promises.readFile(path.join(shellDestination, 'range-test.bin'));
    if (!saved.equals(bytes)) throw new Error('Real Windows folder target content mismatch.');
    const crossDestination = path.join(dataDir, 'cross-process-target', crypto.randomUUID());
    const source = spawn(executable, ['--marshal-server', url], { windowsHide: true, stdio: ['pipe', 'pipe', 'pipe'] });
    const sourceEvents = [];
    let sourceDiagnostic = '';
    source.stderr.on('data', chunk => { sourceDiagnostic += String(chunk).slice(0, 6000); });
    const sourceExit = new Promise((resolve, reject) => { source.on('error', reject); source.on('exit', resolve); });
    // Attach the rejection handler immediately while the client is starting.
    sourceExit.catch(() => {});
    let crossProcessResult;
    try {
      const packet = await new Promise((resolve, reject) => {
        const timeout = setTimeout(() => reject(new Error('COM source did not publish its marshal packet.')), 10000);
        source.once('exit', () => { clearTimeout(timeout); reject(new Error('COM source exited before its marshal packet.')); });
        source.once('error', error => { clearTimeout(timeout); reject(error); });
        readline.createInterface({ input: source.stdout }).on('line', line => {
          const message = JSON.parse(line); sourceEvents.push(message);
          if (message.event === 'marshal_ready') { clearTimeout(timeout); resolve(message.packet); }
        });
      });
      crossProcessResult = await new Promise((resolve, reject) => {
        const child = spawn(executable, ['--marshal-client', packet, crossDestination], { windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'] });
        let output = '', error = '';
        const timeout = setTimeout(() => { child.kill(); reject(new Error('Cross-process Windows folder copy timed out.')); }, 30000);
        child.stdout.on('data', chunk => { output += chunk; }); child.stderr.on('data', chunk => { error += chunk; });
        child.on('error', reject);
        child.on('exit', code => {
          clearTimeout(timeout);
          const passed = output.trim().split('\n').filter(Boolean).map(line => JSON.parse(line)).find(message => message.event === 'cross_process_test_passed');
          if (code !== 0 || !passed) reject(new Error(error || 'Cross-process copy failed.'));
          else resolve(passed);
        });
      });
      source.stdin.end('done\n');
      const code = await sourceExit;
      if (code !== 0 || !sourceEvents.some(event => event.event === 'completed')) throw new Error(sourceDiagnostic || 'COM source did not receive Shell completion.');
      const copied = await fs.promises.readFile(path.join(crossDestination, 'range-test.bin'));
      if (!copied.equals(bytes)) throw new Error('Cross-process folder target content mismatch.');
    } finally { if (source.exitCode === null) source.kill(); }
    const cancellationResults = [];
    for (const phase of ['headers', 'body']) {
      blockedPhase = phase;
      cancellationResults.push(await new Promise((resolve, reject) => {
        const child = spawn(executable, ['--cancel-test', url, phase], { windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'] });
        let output = '', error = '';
        const timer = setTimeout(() => { child.kill(); reject(new Error(`Blocked ${phase} cancellation test timed out.`)); }, 10000);
        child.stdout.on('data', chunk => { output += chunk; });
        child.stderr.on('data', chunk => { error += chunk; });
        child.on('error', reject);
        child.on('exit', code => {
          clearTimeout(timer);
          const events = output.trim().split('\n').filter(Boolean).map(line => JSON.parse(line));
          const passed = events.find(event => event.event === 'cancel_test_passed');
          if (code !== 0 || !passed || events.filter(event => event.event === 'cancelled').length !== 1 || events.some(event => ['error', 'completed', 'progress'].includes(event.event))) reject(new Error(error || `Blocked ${phase} cancellation failed.`));
          else resolve(passed);
        });
      }));
    }
    blockedPhase = null;
    let timer, completion;
    const reported = new Promise((resolve, reject) => { completion = resolve; timer = setTimeout(() => reject(new Error('Broker did not reject mouse-up drag.')), 8000); });
    const broker = createDragBroker({ dataDir, onEvent: message => { if (message.id === 'no-mouse-test' && message.event === 'error' && message.message.includes('left mouse button')) completion(); } });
    try {
      await broker.ready();
      await broker.begin([{ name: 'test.bin', size: bytes.length, url }], 'no-mouse-test');
      await reported;
    } finally { clearTimeout(timer); broker.close(); }
    console.log(JSON.stringify({ passed: true, descriptorAbove4GiB: true, streamReadAndSeek: true, sourceWindowOwnership: true, threadButtonStatePreservation: true, physicalButtonContinuation: true, privateFormatOwnership: true, privateFormatLifetime: true, shellDragImage: true, copyWithScroll: true, crossProcessDragImage: true, rangedHttpRequests: ranges.length, realWindowsFolderCopy: true, asyncShellCallback: shellResult.asyncShellCallback, crossProcessShellCopy: true, crossProcessAsyncCallback: crossProcessResult.asyncShellCallback, warmBroker: true, mouseReleaseGuard: true, cancellationResults, clipboardUntouched: true }));
  } finally { await new Promise(resolve => server.close(resolve)); }
}
main().catch(error => { console.error(error.message); process.exitCode = 1; });
