// Synthetic offline catalogue only. This bridge cannot open a phone or transfer files.
const { contextBridge, ipcRenderer } = require('electron');
const calls = [];
contextBridge.exposeInMainWorld('bridge', {
  request: async (command, args = {}) => {
    calls.push({ command, args });
    return ipcRenderer.invoke('eyecopy:offline-thumbnails-fixture', command, args);
  },
  onEvent: () => {},
  fixtureCalls: () => structuredClone(calls)
});
