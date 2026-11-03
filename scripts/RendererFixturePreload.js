// Isolated renderer fixture. Never talks to a phone, writes media, or invokes a native drag.
const { contextBridge, ipcRenderer } = require('electron');
const pixel = 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jC1sAAAAASUVORK5CYII=';
const calls = [];
const settingsFixture = process.argv.includes('--eyecopy-settings-fixture');
let fixtureAppSettings = { stayInBackground: true, theme: 'ember', updateRepository: '', autoCheckUpdates: true };
let fixtureUpdateStatus = { status: 'unconfigured', message: 'Set a GitHub repository to enable updates.', currentVersion: '1.0.0' };
const apps = [{ id: 'blackmagic', name: 'BlackmagicCam' }, { id: 'obsidian', name: 'Obsidian' }];
const photo = index => ({ id: `photo-${index}`, deviceId: 'fixture-phone', source: 'photos', path: `/DCIM/IMG_${index}.HEIC`, name: `IMG_${index}.HEIC`, kind: 'photo', size: 2000000, modified: '2026-10-01T12:00:00Z' });
const media = [{ id: 'fixture-video', deviceId: 'fixture-phone', source: 'app', appId: 'blackmagic', parentPath: '/Documents/Media', path: '/Documents/Media/sample-video.mov', name: 'sample-video.mov', kind: 'video', size: 1247431888, modified: '2026-10-01T12:00:00Z' }, ...Array.from({ length: 70 }, (_, index) => ({ id: `file-${index}`, deviceId: 'fixture-phone', source: 'app', appId: 'blackmagic', parentPath: '/Documents/Media', path: `/Documents/Media/clip-${index}.txt`, name: `clip-${index}.txt`, kind: 'file', size: 1000000, modified: '2026-10-01T12:00:00Z' }))];
media[0].durationSeconds = 125.25;
const folder = { id: 'folder-media', deviceId: 'fixture-phone', source: 'app', appId: 'blackmagic', parentPath: '/Documents', path: '/Documents/Media', name: 'Media', kind: 'folder' };
media.push({ ...media[0], id: 'fixture-local-video', name: 'local-prores.mov', path: '/Documents/Media/local-prores.mov', localPath: 'C:\\FixtureDownloads\\local-prores.mov' });
delete media.at(-1).durationSeconds;
const photos = Array.from({ length: 90 }, (_, index) => photo(index));
const liveMotion = { ...photo(90), id: 'live-motion', path: '/DCIM/IMG_LIVE.MOV', name: 'IMG_LIVE.MOV', kind: 'video', size: 5000000, durationSeconds: 3 };
const liveStill = { ...photo(90), id: 'live-still', path: '/DCIM/IMG_LIVE.HEIC', name: 'IMG_LIVE.HEIC', livePhoto: { verified: true, video: liveMotion, totalSize: 7000000 } };
const standaloneMotion = { ...liveMotion, id: 'unverified-motion', name: 'UNVERIFIED.MOV', path: '/DCIM/UNVERIFIED.MOV' };
photos.push(liveStill, liveMotion, standaloneMotion);
const legacyPhotos = photos.map(item => { const copy = { ...item }; delete copy.livePhoto; return copy; });
let callback, streamIndex = 0, delayNextPicker = false, resolvePicker, delayMediaCommand, resolveMedia, playableStream = false, resolvePhotoScan, metadataMigrated = false, delayVideoMetadataId, resolveVideoMetadata, movieRevision = 1;
let delayNextDownload = false, resolveDownload, selectedPcReceipt = [];
const missingPcItems = new Set();
const mediaResponse = (command, result) => { if (delayMediaCommand === command) { delayMediaCommand = null; return new Promise(resolve => { resolveMedia = () => resolve(result); }); } return result; };
let transcodedStreams = false;
const movieURL = (compatible, start, local) => transcodedStreams && compatible
  ? `eyecopy-media://fixture-transcoded-${Math.round((Number(start) || 0) * 1000)}-${++streamIndex}/video`
  : playableStream ? `eyecopy-media://fixture-playable-${++streamIndex}/video`
  : local ? `eyecopy-media://fixture-local-${compatible ? 'compatible' : 'original'}/video` : `eyecopy-media://fixture-stream-${++streamIndex}/video`;
const fixtureState = { devices: [{ id: 'fixture-phone', name: 'Test iPhone', online: true, battery: 92, storageTotal: 1e12, storageFree: 5e11, defaultDownloadDir: 'C:\\Videos\\EyeCopy\\Test iPhone', downloadDir: 'C:\\FixtureDownloads' }], selectedDevice: 'fixture-phone', connection: { status: 'ready', serviceAvailable: true }, catalogue: legacyPhotos, apps, jobs: [{ id: 'explorer-fixture', name: 'Drop.mov', total: 1000000000, bytes: 123000000, state: 'transferring', direction: 'download', speed: 150e6 }], downloadDir: 'C:\\FixtureDownloads', usb: { verified: true, negotiatedSpeed: 'SuperSpeedPlus', signalingRate: '10 Gbit/s' } };
Object.assign(fixtureState, { defaultBaseDir: 'C:\\Videos', downloadRoot: 'C:\\Videos\\EyeCopy' });
const itemsFor = args => args.source === 'app' ? args.appId === 'blackmagic' ? args.path === '/Documents/Media' ? media : [folder] : [] : metadataMigrated ? photos : legacyPhotos;
let uploadHomeApp, uploadIndex = 0, deletionPlan;
let nextDeleteSaveWarning;
const locationUndo = [], locationRedo = [];
let locationIndex = 0, savedFixturePhones, defaultPhotoPath;
const defaultCopies = items => {
  const result = structuredClone(items), folder = fixtureState.devices.find(item=>item.id==='fixture-phone')?.defaultDownloadDir;
  const item = result.find(item=>item.id==='photo-0');
  if (item && defaultPhotoPath) { if (folder && defaultPhotoPath.startsWith(folder + '\\')) item.localPath = defaultPhotoPath; else delete item.localPath; }
  for (const file of result) { if (missingPcItems.has(file.id)) delete file.localPath; if (file.livePhoto?.video && missingPcItems.has(file.livePhoto.video.id)) delete file.livePhoto.video.localPath; }
  return result;
};
const changeLocation = (path, permanent = false, explicitRoot) => {
  const phone = fixtureState.devices.find(item => item.id === 'fixture-phone') || { id: null, defaultDownloadDir: fixtureState.downloadRoot };
  const before = { path: fixtureState.downloadDir, defaultPath: phone.defaultDownloadDir, parent: fixtureState.defaultBaseDir, root: fixtureState.downloadRoot };
  const changed = path !== before.path || permanent && path !== before.defaultPath;
  if (changed) {
    const root = permanent ? explicitRoot || path.slice(0, path.lastIndexOf('\\')) : before.root, parent = permanent ? root.slice(0, root.lastIndexOf('\\')) : before.parent;
    const entry = { id: `location-${++locationIndex}`, before, after: { path, defaultPath: permanent ? path : before.defaultPath, root, parent } };
    locationUndo.push(entry); locationRedo.length = 0;
    fixtureState.downloadDir = path; if (permanent) { phone.defaultDownloadDir = path; fixtureState.defaultBaseDir = parent; fixtureState.downloadRoot = root; }
    return { changed: true, historyId: entry.id, path, defaultPath: phone.defaultDownloadDir, deviceId: phone.id };
  }
  return { changed: false, path, defaultPath: phone.defaultDownloadDir, deviceId: phone.id };
};
const deleteResult = (command, result) => {
  const pending = nextDeleteSaveWarning === command;
  nextDeleteSaveWarning = null;
  Object.assign(fixtureState, { stateSavePending: pending, cacheWarning: pending ? 'Deletion finished, but Windows temporarily blocked saving EyeCopy history.' : null });
  return { ...result, stateSavePending: pending, cacheWarning: fixtureState.cacheWarning };
};
const homeFiles = [];
const home = () => ({ deviceId: 'fixture-phone', appId: uploadHomeApp || null, path: uploadHomeApp ? '/Documents/EyeCopy' : null, requiresChoice: !uploadHomeApp, nativeEyeCopy: false, apps, message: 'iOS exposes shared app documents over USB. Choose a location for EyeCopy; the On My iPhone root and Safari Downloads are unavailable.' });
const catalogueFor = args => args.appId === uploadHomeApp && args.path === '/Documents/EyeCopy' ? homeFiles : itemsFor(args);
contextBridge.exposeInMainWorld('bridge', {
  request: async (command, args = {}) => {
    calls.push({ command, args });
    if(command==='get_app_settings') return settingsFixture ? ipcRenderer.invoke('eyecopy:fixture-settings',command,args) : {...fixtureAppSettings};
    if(command==='set_app_settings') {
      if(settingsFixture) return ipcRenderer.invoke('eyecopy:fixture-settings',command,args);
      fixtureAppSettings={...fixtureAppSettings,...args};callback?.({event:'app_settings',data:{...fixtureAppSettings}});return {...fixtureAppSettings};
    }
    if(command==='get_update_status') return {...fixtureUpdateStatus};
    if(command==='check_updates') {fixtureUpdateStatus={status:'available',availableVersion:'1.0.1',currentVersion:'1.0.0',message:'EyeCopy 1.0.1 is available.'};callback?.({event:'update_status',data:{...fixtureUpdateStatus}});return {...fixtureUpdateStatus};}
    if(command==='install_update') return {...fixtureUpdateStatus};
    if(command==='cancel_update') {fixtureUpdateStatus={status:'available',availableVersion:'1.0.1',message:'EyeCopy 1.0.1 is available.'};callback?.({event:'update_status',data:{...fixtureUpdateStatus}});return {...fixtureUpdateStatus};}
    if (command === 'state') return { ...structuredClone(fixtureState), catalogue: defaultCopies(fixtureState.catalogue) };
    if (command === 'list_apps') return apps;
    if (command === 'get_cached_catalogue') return { items: args.deviceId && args.deviceId!=='fixture-phone' ? [] : defaultCopies(catalogueFor(args)), ...(args.source === 'photos' ? { needsLivePhotoScan: true } : {}) };
    if (command === 'scan' && args.source === 'photos') return new Promise(resolve => { resolvePhotoScan = () => { metadataMigrated = true; fixtureState.catalogue = photos; resolve({ items: photos }); }; });
    if (command === 'scan') { Object.assign(fixtureState, { currentSource: args.source, currentAppId: args.appId, currentPath: args.path, catalogue: structuredClone(catalogueFor(args)) }); return { items: catalogueFor(args) }; }
    if (command === 'thumbnail') return args.item.id === 'photo-0' ? { available: false } : { url: pixel, available: true };
    if (command === 'video_metadata') {
      const item = [...media,...photos].find(item=>item.id===args.item.id); if (!item || item.kind!=='video') return {itemId:args.item.id,available:false};
      if (item.id==='fixture-local-video') item.durationSeconds=187.75;
      if (item.id==='fixture-video' && item.durationSeconds===null) item.durationSeconds=125.25;
      const result={itemId:item.id,durationSeconds:item.durationSeconds,available:typeof item.durationSeconds==='number',sourceSignature:{size:item.size,modified:item.modified}};
      if(delayVideoMetadataId===item.id) {delayVideoMetadataId=null;return new Promise(resolve=>{resolveVideoMetadata=options=>{const copy={...result,...options};if(options?.omitSignature) delete copy.sourceSignature;delete copy.omitSignature;resolve(copy);};});}
      return result;
    }
    // Match Engine.preview: a plain downloaded original returns a granted file
    // path without streaming metadata. Video playback should use Engine.stream
    // with preferLocal=true instead, which supplies the seekable local session.
    if (command === 'preview') return mediaResponse(command, args.item.kind === 'video' ? args.compatible ? { url: movieURL(true,args.start,true), kind: 'video', mode: 'compatible', downloaded: true, streaming: false, start: args.start || 0 } : { path: args.item.localPath, url: movieURL(false,0,true), kind: 'video', thumbnail: false } : { url: pixel, kind: 'photo', thumbnail: true });
    if (command === 'stream') { const local = args.preferLocal !== false && Boolean(args.item.localPath); return mediaResponse(command, { url: movieURL(args.compatible,args.start,local), kind: 'video', mode: args.compatible ? 'compatible' : 'original', downloaded: local, streaming: !local, source: local ? 'pc' : 'iphone', seekable: !args.compatible, start: args.start || 0 }); }
    if (command === 'stop_stream') return { stopped: true };
    if (command === 'prepare_drag') return { ready: true };
    if (command === 'pick_folder') return 'C:\\FixtureTarget';
    if (command === 'download') { if(delayNextDownload){delayNextDownload=false;return new Promise(resolve=>{resolveDownload=cancelled=>resolve({jobs:structuredClone(fixtureState.jobs),...(cancelled?{cancelled:true}:{})});});} return { jobs: fixtureState.jobs }; }
    if (command === 'resolve_transfer_items') {
      const all=[...photos,...media,...fixtureState.catalogue], items=[];
      for(const requested of args.items){const item=all.find(item=>item.id===requested.id);if(!item)throw new Error('Fixture item missing');items.push(item);if(item.livePhoto?.verified)items.push(item.livePhoto.video);}
      return {items:defaultCopies(items).map(item=>{const copy={...item};delete copy.livePhoto;return copy;})};
    }
    if(command==='cancel_all_jobs'){let count=0;for(const job of fixtureState.jobs)if(['queued','transferring','paused','waiting','waiting_for_approval'].includes(job.state)){Object.assign(job,{state:'cancelled',awaitingStart:false,awaitingDestination:false,cancelRequested:false});count++;}return{changed:count>0,count,jobs:structuredClone(fixtureState.jobs)};}
    if(command==='clear_all_jobs'){const cleared=fixtureState.jobs.length;fixtureState.jobs=[];return{cleared,retained:0,jobs:[]};}
    if (command === 'clear_finished_jobs') {
      fixtureState.jobs = fixtureState.jobs.filter(job => !['completed', 'failed', 'cancelled'].includes(job.state) || job.remoteCleanupPending || job.localPartRetained);
      return { jobs: structuredClone(fixtureState.jobs) };
    }
    if (command === 'choose_job_folder') {
      const index = fixtureState.jobs.findIndex(item => item.id === args.id);
      if (index >= 0) fixtureState.jobs.splice(index, 1, { ...fixtureState.jobs[index], id: 'confirmed-fixture', state: 'queued', awaitingDestination: false, awaitingStart: false, destination: 'C:\\FixtureDragTarget', message: 'Destination selected. Download queued.' });
      return { jobs: structuredClone(fixtureState.jobs) };
    }
    if (command === 'retry_job') {
      const job = fixtureState.jobs.find(item => item.id === args.id);
      if (job) Object.assign(job, { state: 'queued', awaitingStart: false, localPartRetained: false, message: '' });
      return { jobs: structuredClone(fixtureState.jobs) };
    }
    if (command === 'cancel_job') {
      const job = fixtureState.jobs.find(item => item.id === args.id);
      if (job) Object.assign(job, { state: 'cancelled', awaitingDestination: false, awaitingStart: false });
      return { jobs: structuredClone(fixtureState.jobs) };
    }
    if (command === 'pick_upload_files') { if (delayNextPicker) { delayNextPicker = false; return new Promise(resolve => { resolvePicker = resolve; }); } return ['C:\\FixtureInput\\Test.jpg', 'C:\\FixtureInput\\Test.mov']; }
    if (command === 'files_home') return home();
    if (command === 'configure_files_home') { uploadHomeApp = args.appId; return home(); }
    if (command === 'ensure_files_home') return { ...home(), created: true };
    if (command === 'upload') {
      const batch = ++uploadIndex;
      const jobs = args.localPaths.map((localPath, index) => ({ id: `upload-${batch}-${index}`, deviceId: args.deviceId, name: localPath.split(/[\\/]/).pop(), state: 'queued', direction: 'upload', total: 1000, bytes: 0, fixtureTarget: { appId: args.appId, path: args.path } }));
      fixtureState.jobs.push(...jobs); callback?.({ event: 'jobs', data: structuredClone(fixtureState.jobs) }); return { jobIds: jobs.map(job => job.id) };
    }
    if (command === 'create_folder') { homeFiles.push({ id: `new-folder-${args.name}`, deviceId: args.deviceId, source: 'app', appId: args.appId, parentPath: args.path, path: `${args.path}/${args.name}`, name: args.name, kind: 'folder' }); return { created: true, path: `${args.path}/${args.name}`, name: args.name }; }
    if (['preview_phone_delete','preview_phone_folder_delete'].includes(command)) { deletionPlan = structuredClone(args.items); return { token: command === 'preview_phone_delete' ? 'delete-phone-fixture' : 'delete-folder-fixture', files: deletionPlan, folderCount: deletionPlan.filter(item=>item.kind==='folder').length, fileCount: deletionPlan.filter(item=>item.kind!=='folder').length, bytes: deletionPlan.reduce((sum, file) => sum + (file.size || 0), 0), permanent: true, warning: 'These items will be permanently deleted from the iPhone.' }; }
    if (['confirm_phone_delete','confirm_phone_folder_delete'].includes(command)) { const results = deletionPlan.map(file => ({ id: file.id, appId: file.appId, name: file.name, path: file.path, deleted: true })); const ids = new Set(results.map(file => file.id)); for (const list of [homeFiles, media, fixtureState.catalogue]) for (let index = list.length - 1; index >= 0; index--) if (ids.has(list[index].id)) list.splice(index, 1); return deleteResult(command, { results, deleted: results.length, failed: 0 }); }
    if (command === 'preview_pc_cleanup') return { token: 'cleanup-pc-fixture', deviceId: args.deviceId, path: 'C:\\Videos\\EyeCopy\\Test iPhone', files: [{ path: 'C:\\Videos\\EyeCopy\\Test iPhone\\clip.mov', name: 'clip.mov', size: 1000 }], fileCount: 1, bytes: 1000, permanent: false, warning: 'Only downloaded PC copies in this folder will be moved to Recycle Bin.' };
    if(command==='preview_pc_delete'){deletionPlan=args.items.flatMap(requested=>{const item=[...photos,...media].find(item=>item.id===requested.id);return item?.livePhoto?.verified?[item,item.livePhoto.video]:item?[item]:[];}).filter(item=>item.localPath&&!missingPcItems.has(item.id));return{token:'selected-pc-fixture',files:deletionPlan.map(item=>({name:item.name,path:item.localPath,size:item.size})),fileCount:deletionPlan.length,bytes:deletionPlan.reduce((sum,item)=>sum+item.size,0),path:fixtureState.devices[0].defaultDownloadDir,permanent:false,warning:'Only these selected PC downloads will be moved to Recycle Bin. Device originals and other PC files remain unchanged.'};}
    if(command==='confirm_pc_delete'){selectedPcReceipt=deletionPlan.map(item=>({id:item.id,path:item.localPath}));const results=selectedPcReceipt.map(item=>({...item,deleted:true}));for(const item of deletionPlan){missingPcItems.add(item.id);delete item.localPath;}return{undoToken:'pc-selected-undo',results,deleted:results.length,failed:0};}
    if (command === 'confirm_pc_cleanup') return deleteResult(command, { undoToken: 'pc-undo-fixture', results: [{ path: 'C:\\Videos\\EyeCopy\\Test iPhone\\clip.mov', name: 'clip.mov', deleted: true }], deleted: 1, failed: 0 });
    if (command === 'undo_pc_cleanup') { if(args.token==='pc-selected-undo'){for(const receipt of selectedPcReceipt){const item=[...photos,...media].find(item=>item.id===receipt.id);if(item)item.localPath=receipt.path;missingPcItems.delete(receipt.id);}return{redoToken:'pc-selected-redo',restored:selectedPcReceipt.length,failed:0,results:selectedPcReceipt.map(item=>({...item,restored:true}))};}return { redoToken: 'pc-redo-fixture', restored: 1, failed: 0, results: [{ restored: true }] }; }
    if (command === 'redo_pc_cleanup') {if(args.token==='pc-selected-redo'){for(const receipt of selectedPcReceipt)missingPcItems.add(receipt.id);return{undoToken:'pc-selected-undo',deleted:selectedPcReceipt.length,failed:0,results:selectedPcReceipt.map(item=>({...item,deleted:true}))};}return { undoToken: 'pc-undo-fixture-2', deleted: 1, failed: 0, results: [{ deleted: true }] };}
    if (command === 'reset_download_dir') return changeLocation(fixtureState.devices.find(item => item.id === 'fixture-phone').defaultDownloadDir);
    if (command === 'set_download_dir') return changeLocation(args.path);
    if (command === 'set_default_download_dir') {
      const root = `${args.path}\\EyeCopy`, phone = fixtureState.devices.find(item=>item.id==='fixture-phone');
      return changeLocation(phone ? `${root}\\${phone.name}` : root, true, root);
    }
    if (['undo_download_location','redo_download_location'].includes(command)) {
      const undo = command === 'undo_download_location', source = undo ? locationUndo : locationRedo, entry = source.at(-1);
      if (!entry || args.historyId && args.historyId !== entry.id) throw new Error('Location history changed.');
      source.pop(); (undo ? locationRedo : locationUndo).push(entry);
      const value = entry[undo ? 'before' : 'after']; fixtureState.downloadDir = value.path;
      const phone = fixtureState.devices.find(item=>item.id==='fixture-phone'); if (phone) phone.defaultDownloadDir = value.defaultPath;
      fixtureState.defaultBaseDir = value.parent; fixtureState.downloadRoot = value.root;
      return { changed: true, path: value.path, historyId: entry.id };
    }
    return { ok: true };
  },
  onEvent: listener => { callback = listener; },
  startDrag: payload => { calls.push({ command: 'native_drag', args: payload }); },
  fixtureCalls: () => structuredClone(calls),
  fixtureUpdateStatus: value => {fixtureUpdateStatus=structuredClone(value);callback?.({event:'update_status',data:structuredClone(value)});},
  fixtureFailNextSettingsSave: () => ipcRenderer.invoke('eyecopy:fixture-settings','fail_next_save'),
  fixtureEmit: event => callback?.(event),
  fixtureSetJobs: jobs => { fixtureState.jobs = structuredClone(jobs); callback?.({ event: 'jobs', data: structuredClone(jobs) }); },
  fixtureDelayDownload: () => { delayNextDownload=true; },
  fixtureResolveDownload: cancelled => { resolveDownload?.(cancelled);resolveDownload=null; },
  fixtureMovieLocal: available => {if(available)media[0].localPath='C:\\FixtureDownloads\\Movie.mov';else delete media[0].localPath;fixtureState.catalogue=structuredClone(media);callback?.({event:'catalogue',data:{deviceId:'fixture-phone',source:'app',appId:'blackmagic',path:'/Documents/Media',items:structuredClone(media)}});},
  fixtureAddHiddenFiles: () => {photos.push({...photo(900),id:'hidden-aae',name:'IMG_ADJUSTMENT.AAE',path:'/DCIM/IMG_ADJUSTMENT.AAE',kind:'file',modified:'2026-10-02T12:00:00Z'},{...photo(901),id:'hidden-document',name:'INTERNAL.DAT',path:'/DCIM/INTERNAL.DAT',kind:'file',modified:'2026-10-02T12:00:00Z'});fixtureState.catalogue=structuredClone(photos);callback?.({event:'catalogue',data:{deviceId:'fixture-phone',source:'photos',items:defaultCopies(photos)}});},
  fixtureRemoveLocal: (id, notify=true) => {missingPcItems.add(id);if(notify)callback?.({event:'catalogue',data:{deviceId:'fixture-phone',source:'photos',items:defaultCopies(photos)}});},
  fixtureCompleteLivePhoto: (part, localPath) => {
    const component = part === 'still' ? liveStill : part === 'motion' ? liveMotion : null;
    if (!component || typeof localPath !== 'string') throw new Error('Invalid fixture component completion.');
    component.localPath = localPath;
    callback?.({ event: 'catalogue', data: { deviceId: 'fixture-phone', source: 'photos', items: structuredClone(photos) } });
  },
  fixtureDelayPicker: () => { delayNextPicker = true; },
  fixtureRegisterDefaultPhoto: () => { missingPcItems.delete('photo-0');defaultPhotoPath = `${fixtureState.devices.find(item=>item.id==='fixture-phone').defaultDownloadDir}\\${photos[0].name}`; photos[0].localPath = defaultPhotoPath; fixtureState.catalogue = structuredClone(photos); Object.assign(fixtureState, { currentSource:'photos', currentAppId:null, currentPath:'/DCIM' }); callback?.({ event:'catalogue',data:{deviceId:'fixture-phone',source:'photos',items:defaultCopies(photos)} }); return defaultPhotoPath; },
  fixtureDefaultPhotoPath: () => defaultPhotoPath,
  fixtureDurationEvent: (itemId,durationSeconds) => { const item=[...media,...photos].find(item=>item.id===itemId); if(item) item.durationSeconds=durationSeconds; callback?.({event:'video_metadata',data:{itemId,durationSeconds,available:true,sourceSignature:{size:item?.size,modified:item?.modified}}}); },
  fixtureChangeMovieSource: () => {const item=media.find(item=>item.id==='fixture-video');item.size+=1;item.modified=`2026-10-${String(++movieRevision).padStart(2,'0')}T12:00:00Z`;item.durationSeconds=null;fixtureState.catalogue=structuredClone(media);callback?.({event:'catalogue',data:{deviceId:'fixture-phone',source:'app',appId:'blackmagic',path:'/Documents/Media',items:structuredClone(media)}});return {size:item.size,modified:item.modified};},
  fixtureDelayVideoMetadata: itemId => {delayVideoMetadataId=itemId;},
  fixtureResolveVideoMetadata: options => {resolveVideoMetadata?.(options);resolveVideoMetadata=null;},
  fixtureFuturePhone: () => { fixtureState.devices.push({id:'fixture-future-phone',name:'Future iPhone',online:false,defaultDownloadDir:`${fixtureState.downloadRoot}\\Future iPhone`}); callback?.({event:'state',data:{...structuredClone(fixtureState),catalogue:[]}}); },
  fixtureEmptyPhones: () => { savedFixturePhones = structuredClone({ devices: fixtureState.devices, selectedDevice: fixtureState.selectedDevice, catalogue: fixtureState.catalogue }); Object.assign(fixtureState, { devices: [], selectedDevice: null, catalogue: [], defaultBaseDir: 'C:\\Videos', downloadRoot: 'C:\\Videos\\EyeCopy', downloadDir: '' }); callback?.({ event: 'state', data: structuredClone(fixtureState) }); },
  fixtureRestorePhones: () => { Object.assign(fixtureState, savedFixturePhones); const phone = fixtureState.devices.find(item=>item.id==='fixture-phone'); if (phone) { phone.defaultDownloadDir = `${fixtureState.downloadRoot}\\${phone.name}`; fixtureState.downloadDir = phone.defaultDownloadDir; } callback?.({ event: 'state', data: structuredClone(fixtureState) }); },
  fixtureResolvePicker: () => { resolvePicker?.(['C:\\FixtureInput\\Stale.jpg']); resolvePicker = null; },
  fixturePlayableStream: () => { playableStream = true; },
  fixtureUseTranscodedStreams: value => { transcodedStreams = Boolean(value); },
  fixtureDelayMedia: command => { delayMediaCommand = command; },
  fixtureResolveMedia: () => { resolveMedia?.(); resolveMedia = null; },
  fixtureResolvePhotoScan: () => { resolvePhotoScan?.(); resolvePhotoScan = null; },
  fixtureNextDeleteSaveWarning: command => { nextDeleteSaveWarning = command; },
  fixtureRecoverHistorySave: () => { fixtureState.stateSavePending = false; fixtureState.cacheWarning = null; },
  fixtureCompleteUploads: () => {
    for (const job of fixtureState.jobs) {
      if (job.direction !== 'upload' || job.state !== 'queued') continue;
      Object.assign(job, { state: 'completed', bytes: job.total });
      const target = job.fixtureTarget;
      homeFiles.push({ id: `original-${job.id}`, deviceId: job.deviceId, source: 'app', appId: target.appId, parentPath: target.path, path: `${target.path}/${job.name}`, name: job.name, kind: 'file', size: job.total, modified: '2026-10-12T12:00:00Z' });
    }
    callback?.({ event: 'jobs', data: structuredClone(fixtureState.jobs) });
  },
});
