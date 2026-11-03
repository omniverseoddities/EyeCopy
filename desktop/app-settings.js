'use strict';
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const { normalizeRepository } = require('./update-service');

const DEFAULT_SETTINGS = Object.freeze({ stayInBackground: true, theme: 'ember', updateRepository: '', autoCheckUpdates: true });
const THEMES = new Set(['ember', 'ocean', 'violet', 'forest']);

function readSettings(value, defaultRepository = '') {
  let updateRepository = defaultRepository;
  try { updateRepository = normalizeRepository(typeof value?.updateRepository === 'string' ? value.updateRepository : defaultRepository); } catch { updateRepository = ''; }
  return {
    stayInBackground: typeof value?.stayInBackground === 'boolean' ? value.stayInBackground : true,
    theme: THEMES.has(value?.theme) ? value.theme : 'ember',
    updateRepository,
    autoCheckUpdates: typeof value?.autoCheckUpdates === 'boolean' ? value.autoCheckUpdates : true,
  };
}

function createAppSettings(dataDir, { filesystem = fs, defaultRepository = '' } = {}) {
  const filename = path.join(dataDir, 'desktop-settings.json');
  let settings = readSettings({}, defaultRepository);
  try { settings = readSettings(JSON.parse(filesystem.readFileSync(filename, 'utf8')), defaultRepository); }
  catch { /* Missing or damaged preferences must not prevent startup. */ }
  return {
    get() { return { ...settings }; },
    update(patch) {
      if (!patch || typeof patch !== 'object' || Array.isArray(patch)
          || Object.keys(patch).some(key => !Object.hasOwn(DEFAULT_SETTINGS, key))) throw new Error('Invalid application settings.');
      if (Object.hasOwn(patch, 'stayInBackground') && typeof patch.stayInBackground !== 'boolean') throw new Error('Choose whether EyeCopy stays in the background.');
      if (Object.hasOwn(patch, 'theme') && !THEMES.has(patch.theme)) throw new Error('Choose an available color theme.');
      if (Object.hasOwn(patch, 'autoCheckUpdates') && typeof patch.autoCheckUpdates !== 'boolean') throw new Error('Choose whether EyeCopy checks for updates automatically.');
      const next = { ...settings, ...patch, ...(Object.hasOwn(patch, 'updateRepository') ? { updateRepository: normalizeRepository(patch.updateRepository) } : {}) };
      if (Object.keys(DEFAULT_SETTINGS).every(key => next[key] === settings[key])) return this.get();
      filesystem.mkdirSync(dataDir, { recursive: true });
      const temporary = filename + '.' + crypto.randomUUID() + '.tmp';
      try {
        filesystem.writeFileSync(temporary, JSON.stringify(next, null, 2) + '\n', { encoding: 'utf8', flag: 'wx' });
        filesystem.renameSync(temporary, filename);
      } catch (error) {
        try { filesystem.unlinkSync(temporary); } catch { /* Only this attempt's private temporary file is eligible. */ }
        throw new Error('EyeCopy could not save settings. Your previous settings are still active.', { cause: error });
      }
      settings = next;
      return this.get();
    },
  };
}

function shouldHideOnClose(settings, shuttingDown, trayAvailable = true) {
  return !shuttingDown && trayAvailable && settings.stayInBackground === true;
}

module.exports = { createAppSettings, shouldHideOnClose, DEFAULT_SETTINGS };
