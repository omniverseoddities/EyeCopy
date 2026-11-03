'use strict';

// Older engine records have no public creation time. Remember their first
// ordered arrival so later engine work sorts correctly alongside Explorer jobs.
function createQueueOrder(now = Date.now) {
  const seen = new Map();
  let sequence = 0;
  return jobs => {
    const present = new Set(jobs.map(job => String(job.id)));
    for (const id of seen.keys()) if (!present.has(id)) seen.delete(id);
    return jobs.map(job => {
      const id = String(job.id);
      if (!seen.has(id)) {
        sequence = Math.max(now(), sequence + 0.001);
        seen.set(id, sequence);
      }
      return { ...job, createdAt: Number.isFinite(job.createdAt) ? job.createdAt : seen.get(id) };
    });
  };
}
module.exports = { createQueueOrder };
