'use strict';

async function controlQueue(command, { request, manager, getBackendJobs = () => undefined, setBackendJobs = () => {}, log = () => {} }) {
  if (!['cancel_all_jobs', 'clear_all_jobs'].includes(command)) throw new Error('Unknown queue action.');
  const previousJobs = getBackendJobs();
  let drag = { count: 0 }, dragError;
  try { drag = manager?.cancelAll() || drag; }
  catch (error) { dragError = error; log(`Explorer stop failed: ${error.message}`); }
  // An Explorer helper failure must never prevent cancelling phone transfers.
  const result = await request(command);
  // A worker can finish after the response snapshot was built but before its
  // JSON line reaches us. A newer published event wins over that older reply.
  const published = getBackendJobs();
  const backendJobs = published !== previousJobs && Array.isArray(published) ? published : result.jobs || [];
  setBackendJobs(backendJobs);
  if (command === 'cancel_all_jobs') {
    if (dragError) throw new Error('Phone transfers were stopped, but an Explorer copy could not be stopped. Check its copy window and try again.');
    return { ...result, changed: Boolean(result.changed || drag.changed),
      count: (Number(result.count) || 0) + (Number(drag.count) || 0),
      jobs: [...backendJobs, ...(manager?.jobs() || [])] };
  }
  let cleared = { cleared: 0, blocked: false };
  try { cleared = manager?.clearAll() || cleared; }
  catch (error) { dragError ||= error; log(`Explorer queue cleanup failed: ${error.message}`); }
  const jobs = [...backendJobs, ...(manager?.jobs() || [])];
  const blocked = Array.isArray(result.blocked) ? [...result.blocked] : result.blocked ? [{ reason: 'Transfer cleanup is pending.' }] : [];
  if (cleared.blocked || dragError) blocked.push({ reason: 'An Explorer request is still stopping. Try Clear all again after it stops.' });
  return { ...result, cleared: (Number(result.cleared) || 0) + (Number(cleared.cleared) || 0),
    retained: jobs.length, blocked, jobs };
}

module.exports = { controlQueue };
