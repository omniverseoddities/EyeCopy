'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { createAppSettings, shouldHideOnClose } = require('../desktop/app-settings');
const fixtureRoot = path.resolve(__dirname, '..', 'test-output', 'app-settings');
fs.mkdirSync(fixtureRoot, { recursive: true });
const fixture = () => fs.mkdtempSync(path.join(fixtureRoot, 'case-'));

test('Settings preserve background behavior by default and survive restart independently of the backend database', () => {
  const dir = fixture(), settings = createAppSettings(dir);
  fs.writeFileSync(path.join(dir, 'state.json'), 'sentinel');
  assert.deepEqual(settings.get(), { stayInBackground: true, theme: 'ember', updateRepository: '', autoCheckUpdates: true });
  settings.update({ stayInBackground: false });
  settings.update({ theme: 'ocean' });
  assert.deepEqual(createAppSettings(dir).get(), { stayInBackground: false, theme: 'ocean', updateRepository: '', autoCheckUpdates: true });
  assert.equal(fs.readFileSync(path.join(dir, 'state.json'), 'utf8'), 'sentinel');
  const copy = settings.get(); copy.theme = 'violet';
  assert.equal(settings.get().theme, 'ocean');
});

test('Unknown or invalid updates are rejected while damaged saved preferences recover to defaults', () => {
  const dir = fixture();
  fs.writeFileSync(path.join(dir, 'desktop-settings.json'), '{damaged');
  const settings = createAppSettings(dir);
  for (const patch of [{ theme: 'unknown' }, { stayInBackground: 'false' }, { path: '..' }, null, []]) assert.throws(() => settings.update(patch));
  assert.deepEqual(settings.get(), { stayInBackground: true, theme: 'ember', updateRepository: '', autoCheckUpdates: true });
  fs.writeFileSync(path.join(dir, 'desktop-settings.json'), JSON.stringify({ theme: 'forest', stayInBackground: 'invalid', future: 1 }));
  assert.deepEqual(createAppSettings(dir).get(), { stayInBackground: true, theme: 'forest', updateRepository: '', autoCheckUpdates: true });
});

test('A blocked atomic save retains previous preferences on disk and in memory without leaving a partial file', () => {
  const dir = fixture(), original = createAppSettings(dir);
  original.update({ theme: 'violet' });
  const filesystem = Object.create(fs);
  filesystem.renameSync = () => { throw new Error('locked destination'); };
  const settings = createAppSettings(dir, { filesystem });
  assert.throws(() => settings.update({ stayInBackground: false }), /previous settings/);
  assert.deepEqual(settings.get(), { theme: 'violet', stayInBackground: true, updateRepository: '', autoCheckUpdates: true });
  assert.deepEqual(createAppSettings(dir).get(), settings.get());
  assert.equal(fs.readdirSync(dir).some(name => name.endsWith('.tmp')), false);
});

test('Closing hides only with the saved background option and an available tray, while Quit always exits', () => {
  assert.equal(shouldHideOnClose({ stayInBackground: true }, false, true), true);
  assert.equal(shouldHideOnClose({ stayInBackground: false }, false, true), false);
  assert.equal(shouldHideOnClose({ stayInBackground: true }, true, true), false);
  assert.equal(shouldHideOnClose({ stayInBackground: true }, false, false), false);
});
