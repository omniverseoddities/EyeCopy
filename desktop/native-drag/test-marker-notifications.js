'use strict';
const assert = require('node:assert/strict');
const test = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const { spawn } = require('node:child_process');
const { buildMarkerProbe, markerPayload } = require('../../scripts/MarkerDrop');

test('native exact-nonce claim announces DELETE and exact Unicode parent only after file is gone', async () => {
  const scope = path.resolve(__dirname, '../../test-output/marker-notifications', crypto.randomUUID());
  const folder = path.join(scope, '動画');
  fs.mkdirSync(folder, { recursive: true });
  const executable = await buildMarkerProbe(scope, { preferBundled: false });
  async function claim(name) {
    return new Promise((resolve, reject) => {
      const child = spawn(executable, ['--claim', folder, name], { windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'] });
      let text = '', errors = '';
      child.stdout.on('data', value => { text += value; }); child.stderr.on('data', value => { errors += value; });
      child.on('error', reject);
      child.on('exit', code => { try { assert.equal(code, 0, errors || text); resolve(JSON.parse(text)); } catch (error) { reject(error); } });
    });
  }
  const name = `_EyeCopy_drop_${crypto.randomBytes(16).toString('hex')}.txt`, file = path.join(folder, name);
  fs.writeFileSync(file, markerPayload(name));
  const unrelated = path.join(folder, 'keep-user-file.txt'); fs.writeFileSync(unrelated, 'keep');
  assert.deepEqual(await claim(name), { claimed: true, shellDeleteNotificationSent: true, shellDirectoryNotificationSent: true });
  assert.equal(fs.existsSync(file), false); assert.equal(fs.readFileSync(unrelated, 'utf8'), 'keep');
  const other = `_EyeCopy_drop_${crypto.randomBytes(16).toString('hex')}.txt`, otherFile = path.join(folder, other);
  fs.writeFileSync(otherFile, markerPayload(name));
  assert.deepEqual(await claim(other), { claimed: false, shellDeleteNotificationSent: false, shellDirectoryNotificationSent: false });
  assert.equal(fs.existsSync(otherFile), true, 'A different nonce cannot consume the requested ticket.');
  fs.writeFileSync(otherFile, markerPayload(other));
  assert.deepEqual(await claim(other), { claimed: true, shellDeleteNotificationSent: true, shellDirectoryNotificationSent: true });
  assert.equal(fs.existsSync(otherFile), false);
  assert.deepEqual(await claim(name), { claimed: false, shellDeleteNotificationSent: false, shellDirectoryNotificationSent: false });
  const wrong = markerPayload(name); wrong[0] ^= 1; fs.writeFileSync(file, wrong);
  assert.deepEqual(await claim(name), { claimed: false, shellDeleteNotificationSent: false, shellDirectoryNotificationSent: false });
  assert.ok(fs.readFileSync(file).equals(wrong));
  fs.writeFileSync(file, markerPayload(name));
  const held = fs.openSync(file, 'r');
  try { assert.deepEqual(await claim(name), { claimed: false, shellDeleteNotificationSent: false, shellDirectoryNotificationSent: false }); }
  finally { fs.closeSync(held); }
  assert.equal(fs.existsSync(file), true);
  assert.deepEqual(await claim(name), { claimed: true, shellDeleteNotificationSent: true, shellDirectoryNotificationSent: true });
  assert.equal(fs.existsSync(file), false); assert.equal(fs.readFileSync(unrelated, 'utf8'), 'keep');
});
