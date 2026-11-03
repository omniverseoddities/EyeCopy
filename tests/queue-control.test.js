'use strict';
const assert = require('node:assert/strict');
const test = require('node:test');
const { controlQueue } = require('../desktop/queue-control');

test('Stop all revokes pending Explorer work before stopping engine work and merges both queues', async () => {
  const order = [], native = [{ id: 'explorer-1', state: 'cancelled' }], backend = [{ id: 'phone-1', state: 'transferring', cancelRequested: true }];
  let observed;
  const result = await controlQueue('cancel_all_jobs', {
    manager: { cancelAll() { order.push('drag'); return { count: 1, changed: true }; }, jobs: () => native },
    request: async command => { order.push(command); return { count: 1, changed: true, jobs: backend }; },
    setBackendJobs: value => { observed = value; }
  });
  assert.deepEqual(order, ['drag', 'cancel_all_jobs']);
  assert.equal(result.count, 2); assert.equal(observed, backend);
  assert.deepEqual(result.jobs, [...backend, ...native]);
});

test('Clear all revokes preparations and waits for engine cleanup before removing terminal drag entries', async () => {
  const order = [];
  const result = await controlQueue('clear_all_jobs', {
    manager: { cancelAll() { order.push('revoke'); }, clearAll() { order.push('clear-drag'); return { cleared: 1 }; }, jobs: () => [] },
    request: async command => { order.push(command); return { cleared: 2, jobs: [], blocked: [] }; }
  });
  assert.deepEqual(order, ['revoke', 'clear_all_jobs', 'clear-drag']);
  assert.equal(result.cleared, 3); assert.equal(result.retained, 0); assert.deepEqual(result.blocked, []);
});

test('Clear all retains active cleanup records from both owners and reports actual retained entries', async () => {
  const result = await controlQueue('clear_all_jobs', {
    manager: { cancelAll() {}, clearAll: () => ({ cleared: 0, retained: 1, blocked: true }), jobs: () => [{ id: 'explorer-pending' }] },
    request: async () => ({ cleared: 4, retained: 1, jobs: [{ id: 'phone-stopping' }], blocked: [{ id: 'phone-stopping', reason: 'Stopping' }] })
  });
  assert.equal(result.retained, 2); assert.equal(result.cleared, 4); assert.equal(result.blocked.length, 2);
});

test('A native helper failure does not prevent cancelling the engine and is reported honestly', async () => {
  let stopped = false, observed;
  await assert.rejects(controlQueue('cancel_all_jobs', {
    manager: { cancelAll() { throw new Error('Disconnected native pipe'); } },
    request: async () => { stopped = true; return { jobs: [] }; }, setBackendJobs: value => { observed = value; }
  }), /Explorer copy could not be stopped/);
  assert.equal(stopped, true); assert.deepEqual(observed, []);
});

test('A newer terminal jobs event cannot be overwritten by an older Stop all response snapshot', async () => {
  let published = [{ id: 'phone-1', state: 'transferring' }];
  const terminal = [{ id: 'phone-1', state: 'cancelled' }];
  const result = await controlQueue('cancel_all_jobs', {
    getBackendJobs: () => published, setBackendJobs: value => { published = value; },
    request: async () => {
      published = terminal;
      return { jobs: [{ id: 'phone-1', state: 'transferring', cancelRequested: true }] };
    }
  });
  assert.equal(published, terminal); assert.deepEqual(result.jobs, terminal);
});
