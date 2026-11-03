// Render the production interface with synthetic media and device data only.
// Run with the installed Electron executable; no phone or application profile is used.
const { app, BrowserWindow, ipcMain } = require('electron');
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const assert = require('node:assert/strict');

const root = path.resolve(__dirname, '..');
const output = path.join(root, 'test-output', 'screenshot', crypto.randomUUID());
fs.mkdirSync(output, { recursive: true });
app.setPath('userData', path.join(output, 'profile'));
app.commandLine.appendSwitch('force-device-scale-factor', '1');
process.env.TEMP = output;
process.env.TMP = output;

// Code-drawn scenic artwork avoids personal media and external image downloads.
function artwork(scene, index) {
  const palettes = [
    ['#133f56','#e6a575','#e7c69a','#28657b','#123649'],
    ['#22234c','#d87571','#f5b286','#685a72','#303151'],
    ['#675d74','#edb06c','#f5d798','#aa7660','#6c5767'],
    ['#173745','#88a59b','#b6ccad','#355f60','#173b47'],
    ['#152241','#9b7693','#f2b59a','#4d547a','#262f52'],
    ['#152e41','#79948a','#e1be84','#45696b','#284353'],
  ];
  const [sky, haze, sun, middle, front] = palettes[index % palettes.length];
  let shapes;
  if (scene === 'coast') shapes = `
    <path d="M0 238Q140 207 286 240T800 211V500H0Z" fill="${middle}"/>
    <path d="M800 245Q574 238 575 294T366 374Q188 431 106 500H800Z" fill="${front}"/>
    <path d="M800 268Q616 257 619 303T399 398Q240 438 183 500" fill="none" stroke="${sun}" stroke-width="42"/>
    <path d="M0 358Q149 329 267 350M69 399Q203 368 341 397M30 440Q124 421 201 429" fill="none" stroke="#cde7e6" stroke-opacity=".34" stroke-width="3"/>
    <path d="M649 418l35-13 29 24-27 21-39-5Z" fill="${front}"/>`;
  else if (scene === 'dunes') shapes = `
    <path d="M0 300Q214 134 400 280T800 255V500H0Z" fill="${middle}"/>
    <path d="M0 421Q238 340 405 297Q605 430 800 362V500H0Z" fill="${sun}"/>
    <path d="M0 500Q229 412 405 297Q432 406 800 485V500Z" fill="${front}"/>
    <path d="M405 297Q278 382 99 421" fill="none" stroke="#fff0d5" stroke-opacity=".5" stroke-width="2"/>`;
  else if (scene === 'forest') shapes = `
    <path d="M0 282L139 173 292 267 436 141 656 262 800 197V500H0Z" fill="${middle}"/>
    <path d="M0 347Q260 289 800 359V500H0Z" fill="${front}"/>
    ${Array.from({length:16},(_,n)=>{const x=n*59-20,h=84+(n%4)*22,y=337+(n%3)*29;return `<path d="M${x} ${y-h}l${h*.38} ${h}h-${h*.76}Z" fill="${n%3 ? '#153a3d' : '#265053'}"/><path d="M${x} ${y-10}v35" stroke="#143035" stroke-width="5"/>`;}).join('')}
    <path d="M486 500Q418 426 456 374Q470 350 463 332" fill="none" stroke="${haze}" stroke-width="22" opacity=".65"/>`;
  else if (scene === 'city') shapes = `
    <path d="M0 332Q213 287 800 327V500H0Z" fill="${middle}"/>
    ${Array.from({length:14},(_,n)=>{const x=n*63-12,h=70+(n*37)%146;return `<rect x="${x}" y="${356-h}" width="51" height="${h+71}" fill="${n%2 ? front : '#253b52'}"/>${[0,1,2,3].map(r=>`<path d="M${x+12} ${369-h+r*24}h8m9 0h8" stroke="${sun}" stroke-opacity="${.4+(r%2)*.3}" stroke-width="5"/>`).join('')}`;}).join('')}
    <path d="M0 425H800V500H0Z" fill="#162d42"/>
    <path d="M98 444h187m35 11h180m-28 23h216" stroke="${sun}" stroke-width="3" opacity=".35"/>`;
  else shapes = `
    <path d="M0 320L172 121 335 319 509 155 800 345V500H0Z" fill="${middle}"/>
    <path d="M109 194l63-73 77 94-70-36-25 33Z" fill="#e8ded6" opacity=".8"/>
    <path d="M462 210l47-55 78 83-65-28-22 18Z" fill="#e8ded6" opacity=".7"/>
    <path d="M0 371L179 275 336 387 590 297 800 389V500H0Z" fill="${front}"/>
    <path d="M0 443Q276 401 800 458V500H0Z" fill="#162b38" opacity=".9"/>`;
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="960" height="600" viewBox="0 0 800 500"><defs><linearGradient id="sky" x2="0" y2="1"><stop stop-color="${sky}"/><stop offset="1" stop-color="${haze}"/></linearGradient><radialGradient id="glow"><stop stop-color="${sun}" stop-opacity=".4"/><stop offset="1" stop-color="${sun}" stop-opacity="0"/></radialGradient><linearGradient id="shade" x2="0" y2="1"><stop stop-color="#fff" stop-opacity="0"/><stop offset="1" stop-color="#081623" stop-opacity=".16"/></linearGradient></defs><path fill="url(#sky)" d="M0 0H800V500H0Z"/><circle cx="${590-index%3*120}" cy="143" r="156" fill="url(#glow)"/><circle cx="${590-index%3*120}" cy="143" r="38" fill="${sun}"/>${shapes}<path fill="url(#shade)" d="M0 0H800V500H0Z"/></svg>`;
  return `data:image/svg+xml;base64,${Buffer.from(svg).toString('base64')}`;
}

app.whenReady().then(async () => {
  const names = [
    ['Coastline.mov','video','coast',134,1620000000],
    ['Golden hour.heic','photo','dunes',null,6800000],
    ['Alpine trail.mov','video','mountain',92,935000000],
    ['Forest light.jpg','photo','forest',null,4200000],
    ['Harbor lights.mov','video','city',48,514000000],
    ['Open road.mov','video','mountain',156,2140000000],
    ['Quiet morning.heic','photo','forest',null,5100000],
    ['Desert study.mov','video','dunes',63,782000000],
    ['Evening skyline.jpg','photo','city',null,3800000],
    ['Tide pools.mov','video','coast',37,416000000],
    ['Mountain air.heic','photo','mountain',null,5900000],
    ['Blue hour.mov','video','city',84,1100000000],
  ];
  const media = names.map(([name,kind,scene,durationSeconds,size],i)=>({
    id:`demo-media-${i}`,deviceId:'demo-device',source:'photos',name,kind,size,
    path:`/DCIM/100APPLE/${name}`, modified:new Date(Date.UTC(2026,8,18,17-i,24)).toISOString(),
    thumbnailUrl:artwork(scene,i), ...(kind==='video'?{durationSeconds}:{}),
    ...(i===2||i===5||i===8?{localPath:`C:\\Videos\\EyeCopy\\Studio device\\${name}`}:{})
  }));
  const live = {id:'demo-live-motion',deviceId:'demo-device',source:'photos',name:'Golden hour.mov',kind:'video',size:3900000,durationSeconds:3,path:'/DCIM/100APPLE/Golden hour.mov',modified:media[1].modified};
  media[1].livePhoto={verified:true,video:live,totalSize:media[1].size+live.size};
  const device = {id:'demo-device',name:'Studio device',online:true,productVersion:'26.0',battery:86,storageTotal:512000000000,storageFree:214000000000,defaultDownloadDir:'C:\\Videos\\EyeCopy\\Studio device'};
  const state={devices:[device],selectedDevice:device.id,connection:{status:'ready',serviceAvailable:true},
    catalogue:media,apps:[],jobs:[{id:'demo-transfer',name:'Alpine trail.mov',direction:'download',state:'completed',bytes:935000000,total:935000000,speed:0,destination:device.defaultDownloadDir}],
    downloadDir:device.defaultDownloadDir,downloadRoot:'C:\\Videos\\EyeCopy',defaultBaseDir:'C:\\Videos',usb:{negotiatedSpeed:'SuperSpeedPlus',signalingRate:'10 Gbit/s',verified:true}};
  let win;
  const errors=[];
  ipcMain.handle('eyecopy:screenshot',(_event,command,args)=>{
    if(command==='get_app_settings')return{stayInBackground:true,theme:'ember'};
    if(command==='state')return state;
    if(command==='get_cached_catalogue')return{items:media,needsLivePhotoScan:false};
    if(command==='prepare_drag')return{ok:true};
    if(command==='thumbnail')return{available:true,url:media.find(i=>i.id===args.item.id)?.thumbnailUrl};
    throw new Error(`Screenshot fixture forbids ${command}`);
  });
  try {
    win=new BrowserWindow({show:false,width:1560,height:1000,useContentSize:true,frame:false,webPreferences:{preload:path.join(__dirname,'MakeScreenshotPreload.js'),contextIsolation:true,nodeIntegration:false,sandbox:false,backgroundThrottling:false}});
    win.webContents.on('console-message',(_event,level,message)=>{if(level>=3)errors.push(message);});
    await win.loadFile(path.join(root,'ui','index.html'));
    const js=source=>win.webContents.executeJavaScript(source);
    const wait=ms=>new Promise(resolve=>setTimeout(resolve,ms));
    const until=async condition=>{for(let n=0;n<100;n++){if(await js(condition))return;await wait(50);}throw new Error(`Timed out: ${condition}`);};
    await until(`document.querySelectorAll('#catalogue .file-card').length===12`);
    await until(`Array.from(document.querySelectorAll('#catalogue img')).every(img=>img.complete&&img.naturalWidth>0)`);
    await js(`document.querySelector('[data-key="demo-media-0"]').click()`);
    await until(`document.querySelector('#inspector-content .inspector-name')?.textContent==='Coastline.mov'`);
    await until(`document.querySelector('#inspector-content img')?.complete`);
    await wait(200);
    assert.deepEqual(errors,[]);
    assert.equal(await js(`document.documentElement.scrollWidth>innerWidth`),false);
    fs.mkdirSync(path.join(root,'assets'),{recursive:true});
    const image=await win.webContents.capturePage();
    fs.writeFileSync(path.join(root,'assets','screenshot.png'),image.toPNG());
    fs.writeFileSync(path.join(output,'report.json'),JSON.stringify({passed:true,syntheticOnly:true,productionUI:true,productionStyleChanges:false,dimensions:image.getSize(),consoleErrors:errors},null,2));
    console.log(JSON.stringify({passed:true,syntheticOnly:true,productionUI:true,screenshot:'assets/screenshot.png',dimensions:image.getSize()}));
    app.exit(0);
  }catch(error){console.error(error.stack);app.exit(1);}
});
