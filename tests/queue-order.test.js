'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { createQueueOrder } = require('../desktop/queue-order');

test('Historical jobs retain array order and stable times across progress updates', () => {
  let clock = 1000;
  const order = createQueueOrder(() => clock);
  const first = order([{ id: 'old-1' }, { id: 'old-2' }]);
  clock = 2000;
  const updated = order([{ id: 'old-1', bytes: 9 }, { id: 'old-2', state: 'completed' }]);
  assert.ok(first[1].createdAt > first[0].createdAt);
  assert.deepEqual(updated.map(j => j.createdAt), first.map(j => j.createdAt));
});
test('New engine work sorts after an earlier Explorer request even though merge puts engine jobs first', () => {
  let clock = 1000;
  const order = createQueueOrder(() => clock);
  const initial = order([{ id: 'old-engine' }, { id: 'old-explorer' }]);
  clock = 2000;
  const merged = order([{ id: 'old-engine' }, { id: 'new-engine' }, { id: 'old-explorer' }]);
  assert.equal(merged[2].createdAt, initial[1].createdAt);
  assert.deepEqual([...merged].sort((a,b) => b.createdAt-a.createdAt).map(j=>j.id), ['new-engine','old-explorer','old-engine']);
});
test('Authoritative creation times are retained without mutating the original job', () => {
  const job = { id: 'timed', createdAt: 42 };
  const result = createQueueOrder(() => 1000)([job]);
  assert.equal(result[0].createdAt, 42); assert.notEqual(result[0], job); assert.deepEqual(job, { id: 'timed', createdAt: 42 });
});
