const test = require('node:test');
const assert = require('node:assert/strict');
const { createUpdateInstallController, requireUpdateOperationAllowed } = require('../desktop/update-install');

function fixture(patch = {}) {
  const calls = [], candidate = { path: 'synthetic-setup.exe', version: '1.0.1', repository: 'example/EyeCopy' };
  const state = { status: 'available' };
  const options = { packaged: () => true,
    service: { get: () => ({ ...state }), download: async () => { calls.push('download'); state.status = 'ready'; }, installer: async () => { calls.push('verify'); return candidate; } },
    requireIdle: async () => { calls.push('idle'); }, confirm: async () => { calls.push('confirm'); return true; }, ready: () => { calls.push('quit-and-open-setup'); }, ...patch };
  return { calls, state, options, controller: createUpdateInstallController(options) };
}

test('installation verifies the candidate and idle state again after confirmation', async () => {
  const f = fixture(); assert.equal((await f.controller.install()).status, 'installing');
  assert.deepEqual(f.calls, ['idle', 'download', 'idle', 'verify', 'confirm', 'idle', 'verify', 'quit-and-open-setup']);
});
test('active transfers reject installation before any download or quit', async () => {
  const f = fixture({ requireIdle: async () => { throw new Error('Active transfer'); } });
  await assert.rejects(f.controller.install(), /Active transfer/); assert.deepEqual(f.calls, []);
});
test('source checkouts do not invoke the installer', async () => {
  const f = fixture({ packaged: () => false }); await assert.rejects(f.controller.install(), /Windows package/); assert.deepEqual(f.calls, []);
});
test('cancelling confirmation leaves the application and verified update ready', async () => {
  const f = fixture({ confirm: async () => false }); assert.equal((await f.controller.install()).status, 'ready'); assert(!f.calls.includes('quit-and-open-setup'));
});
test('a transfer started during the confirmation prevents applying the update', async () => {
  let busy = false;
  const f = fixture({ requireIdle: async () => { if (busy) throw new Error('Transfer started'); }, confirm: async () => { busy = true; return true; } });
  await assert.rejects(f.controller.install(), /Transfer started/); assert(!f.calls.includes('quit-and-open-setup'));
});
test('revoked or modified installers after confirmation never reach the handoff', async () => {
  const f = fixture(); let checks = 0;
  f.options.service.installer = async () => { if (++checks > 1) throw new Error('Repository changed'); return {}; };
  await assert.rejects(f.controller.install(), /Repository changed/); assert(!f.calls.includes('quit-and-open-setup'));
});
test('duplicate installation requests cannot create concurrent confirmations', async () => {
  let resolve;
  const f = fixture({ confirm: () => new Promise(done => { resolve = done; }) });
  const first = f.controller.install(); for (let i = 0; i < 20 && !resolve; i++) await new Promise(done => setImmediate(done));
  await assert.rejects(f.controller.install(), /already/); resolve(false); await first;
});
test('failed verification leaves the app running and allows a later retry', async () => {
  const f = fixture(); const verify = f.options.service.installer;
  f.options.service.installer = async () => { throw new Error('Checksum failed'); };
  await assert.rejects(f.controller.install(), /Checksum failed/); assert(!f.calls.includes('quit-and-open-setup'));
  f.options.service.installer = verify; await f.controller.install(); assert(f.calls.includes('quit-and-open-setup'));
});

test('update preparation gates file mutations and native drags while cancellation and status stay available', async () => {
  let release;
  const f = fixture({ confirm: () => new Promise(resolve => { release = resolve; }) });
  const preparation = f.controller.install();
  for (let i = 0; i < 20 && !release; i++) await new Promise(resolve => setImmediate(resolve));
  assert.equal(f.controller.busy(), true);
  for (const command of ['download', 'upload', 'queue_download', 'native_drag', 'prepare_drag', 'retry_job', 'confirm_pc_delete', 'create_folder']) assert.throws(() => requireUpdateOperationAllowed(command, f.controller.busy()), /update is being prepared/);
  for (const command of ['state', 'get_update_status', 'cancel_update', 'cancel_job', 'cancel_all_jobs']) assert.doesNotThrow(() => requireUpdateOperationAllowed(command, f.controller.busy()));
  release(false); await preparation; assert.equal(f.controller.busy(), false);
  assert.doesNotThrow(() => requireUpdateOperationAllowed('download', f.controller.busy()));
});
