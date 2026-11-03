const { contextBridge, ipcRenderer, webUtils } = require('electron');
contextBridge.exposeInMainWorld('bridge', {
  request: (command, args = {}) => ipcRenderer.invoke('bridge:request', command, args),
  onEvent: callback => {
    const handler = (_event, message) => callback(message);
    ipcRenderer.on('bridge:event', handler);
    return () => ipcRenderer.removeListener('bridge:event', handler);
  },
  pathsForFiles: files => Array.from(files).map(file => webUtils.getPathForFile(file)).filter(Boolean),
  startDrag: payload => ipcRenderer.send('bridge:drag', payload)
});
