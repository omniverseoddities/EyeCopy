// This illustration-only bridge cannot reach device services or user profiles.
const { contextBridge,ipcRenderer }=require('electron');
contextBridge.exposeInMainWorld('bridge',{
  request:(command,args={})=>ipcRenderer.invoke('eyecopy:screenshot',command,args),
  onEvent:()=>{}
});
