'use strict';
// Isolated real-media integration: no mouse, device service, or user originals.
const { app, BrowserWindow, protocol, nativeImage } = require('electron');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const { pathToFileURL } = require('node:url');
const { installMediaPermissions } = require('../desktop/media-permissions');

const root = path.resolve(__dirname, '..');
assert(fs.existsSync(path.join(root, 'backend', 'engine.py')), 'Run this fixture from its source project.');
const output = path.join(root, 'test-output');
fs.mkdirSync(output, { recursive: true });
const fixture = fs.mkdtempSync(path.join(output, 'media-seek-renderer-'));
const temporary = path.join(fixture, 'temp');
fs.mkdirSync(temporary);
process.env.TEMP = temporary;
process.env.TMP = temporary;
const reportFile = path.join(output, 'media-seek-renderer.json');
const ffmpeg = path.join(root, 'vendor', 'ffmpeg', 'ffmpeg.exe');
const source = path.join(fixture, 'synthetic-three-scenes.mp4');
const requests = [];
const report = { passed: false, realDeviceAccess: false, originalDownloadsCreated: false,
  operatingSystemMouseInput: false, actualFFmpegMedia: true, checks: {}, scenes: [] };

function encode(args) {
  const result = spawnSync(ffmpeg, args, { windowsHide: true, encoding: null,
    timeout: 20000, maxBuffer: 16 * 1024 * 1024 });
  assert.equal(result.status, 0, result.error?.message || result.stderr?.toString());
  return result.stdout;
}

encode(['-y', '-hide_banner', '-loglevel', 'error',
  '-f', 'lavfi', '-i', 'color=c=red:s=320x180:r=25:d=4',
  '-f', 'lavfi', '-i', 'color=c=green:s=320x180:r=25:d=4',
  '-f', 'lavfi', '-i', 'color=c=blue:s=320x180:r=25:d=4',
  '-filter_complex', '[0:v][1:v][2:v]concat=n=3:v=1:a=0[out]', '-map', '[out]',
  '-c:v', 'libx264', '-preset', 'ultrafast', '-g', '300', '-keyint_min', '300',
  '-sc_threshold', '0', '-pix_fmt', 'yuv420p', '-movflags', '+faststart', source]);
const original = fs.readFileSync(source);
const compatible = new Map();
function compatibleBytes(start) {
  if (!compatible.has(start)) compatible.set(start, encode([
    '-hide_banner', '-loglevel', 'error', '-nostdin', '-threads', '2',
    '-ss', String(start), '-i', source, '-map', '0:v:0', '-map', '0:a:0?',
    '-vf', 'scale=320:180:force_original_aspect_ratio=decrease:force_divisible_by=2',
    '-r', '30', '-c:v', 'libx264', '-preset', 'ultrafast', '-tune', 'zerolatency',
    '-crf', '24', '-threads', '2', '-pix_fmt', 'yuv420p',
    '-movflags', '+frag_keyframe+empty_moov+default_base_moof', '-frag_duration', '500000',
    '-f', 'mp4', 'pipe:1']));
  return compatible.get(start);
}

protocol.registerSchemesAsPrivileged([{ scheme: 'eyecopy-media',
  privileges: { standard: true, secure: true, supportFetchAPI: true, stream: true } }]);
app.setPath('userData', path.join(output, 'renderer-media-seek-profile'));

app.whenReady().then(async () => {
  const window = new BrowserWindow({ width: 1480, height: 950, show: false,
    webPreferences: { preload: path.join(__dirname, 'RendererFixturePreload.js'),
      contextIsolation: true, nodeIntegration: false, sandbox: false,
      backgroundThrottling: false } });
  const consoleErrors = [];
  window.webContents.on('console-message', (_event, level, message) => {
    if (level >= 3) consoleErrors.push(message);
  });
  const uiURL = pathToFileURL(path.join(root, 'ui', 'index.html')).href;
  installMediaPermissions(window, uiURL);
  protocol.handle('eyecopy-media', request => {
    const url = new URL(request.url), range = request.headers.get('Range');
    const transcoded = /^fixture-transcoded-(\d+)-\d+$/.exec(url.hostname);
    if (transcoded) {
      const start = Number(transcoded[1]) / 1000, bytes = compatibleBytes(start);
      requests.push({ kind: 'compatible', start, range, status: 200, byteRangeSupported: false });
      let sent = false;
      const body = new ReadableStream({ pull(controller) {
        if (!sent) { sent = true; controller.enqueue(bytes); }
        controller.close();
      } });
      return new Response(body, { status: 200, headers: {
        'Content-Type': 'video/mp4', 'Cache-Control': 'no-store' } });
    }
    if (url.hostname.startsWith('fixture-playable-')) {
      const match = /^bytes=(\d*)-(\d*)$/.exec(range || '');
      let start = 0, end = original.length - 1;
      if (match) {
        if (!match[1]) start = Math.max(0, original.length - Number(match[2]));
        else { start = Number(match[1]); if (match[2]) end = Math.min(end, Number(match[2])); }
      }
      if (start > end || start >= original.length) return new Response(null, { status: 416,
        headers: { 'Content-Range': `bytes */${original.length}` } });
      requests.push({ kind: 'original', range, start, end, status: match ? 206 : 200 });
      return new Response(original.subarray(start, end + 1), { status: match ? 206 : 200,
        headers: { 'Content-Type': 'video/mp4', 'Content-Length': String(end - start + 1),
          'Accept-Ranges': 'bytes', ...(match ? { 'Content-Range': `bytes ${start}-${end}/${original.length}` } : {}) } });
    }
    return new Response('Unknown isolated fixture media', { status: 404 });
  });

  const evaluate = (code, userGesture = false) => window.webContents.executeJavaScript(code, userGesture);
  const wait = delay => new Promise(resolve => setTimeout(resolve, delay));
  async function until(code, timeout = 12000) {
    const deadline = Date.now() + timeout;
    while (Date.now() < deadline) {
      const value = await evaluate(code);
      if (value) return value;
      await wait(35);
    }
    throw new Error('Timed out waiting for ' + code);
  }
  const calls = () => evaluate('window.bridge.fixtureCalls()');
  async function click(selector) {
    await until(`document.querySelector(${JSON.stringify(selector)})`);
    await evaluate(`document.querySelector(${JSON.stringify(selector)}).click()`);
  }
  async function seek(seconds) {
    await evaluate(`(() => { const slider=document.querySelector('#video-position');
      slider.value=${seconds}; slider.dispatchEvent(new Event('input',{bubbles:true}));
      slider.dispatchEvent(new Event('change',{bubbles:true})); })()`);
  }
  async function ready(start) {
    const host = `fixture-transcoded-${Math.round(start * 1000)}-`;
    await until(`(() => { const v=document.querySelector('#inspector-content video');
      return v?.readyState>=2 && !v.seeking && v.currentSrc.includes(${JSON.stringify(host)})
        && Math.abs(Number(document.querySelector('#video-position')?.value)-${start})<.2; })()`);
  }
  async function scene(label, channel) {
    // HAVE_CURRENT_DATA means a frame is decoded, not that Chromium has sent it
    // to the compositor. A paused hidden window may otherwise capture the
    // preceding background even though the video becomes visible immediately
    // afterward. Wait for the current source's actual presentation callback.
    await until(`(() => { const v=document.querySelector('#inspector-content video'), frame=v?.fixturePresentedFrame;
      return frame?.src===v.currentSrc && frame.presentedFrames>0 && Math.abs(frame.mediaTime-v.currentTime)<.25; })()`);
    await evaluate('new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)))');
    const bounds = await evaluate(`(() => {const r=document.querySelector('#inspector-content video').getBoundingClientRect();
      return {x:r.left+r.width/2,y:r.top+r.height/2,viewportWidth:innerWidth,viewportHeight:innerHeight};})()`);
    const capture = await window.webContents.capturePage(undefined, { stayHidden: true, stayAwake: true });
    // Decode at scale1 so Windows150% native-image representations cannot make
    // a CSS/DIP coordinate sample an unrelated sidebar pixel.
    const image = nativeImage.createFromBuffer(capture.toPNG(), { scaleFactor: 1 });
    const bitmap = image.toBitmap({ scaleFactor: 1 }), size = image.getSize(1);
    assert.equal(bitmap.length, size.width * size.height * 4);
    const x = Math.min(size.width - 1, Math.max(0, Math.floor(bounds.x / bounds.viewportWidth * size.width)));
    const y = Math.min(size.height - 1, Math.max(0, Math.floor(bounds.y / bounds.viewportHeight * size.height)));
    const offset = (y * size.width + x) * 4;
    const rgb = [bitmap[offset + 2], bitmap[offset + 1], bitmap[offset]];
    const snapshot = await evaluate(`(() => {const v=document.querySelector('#inspector-content video');
      return {src:v.currentSrc,time:v.currentTime,paused:v.paused,volume:v.volume,rate:v.playbackRate,
        presentedMediaTime:v.fixturePresentedFrame?.mediaTime,presentedFrames:v.fixturePresentedFrame?.presentedFrames,
        timeline:Number(document.querySelector('#video-position').value),
        sameVideo:!window.mediaVideo||v===window.mediaVideo,
        sameWrapper:!window.mediaWrapper||document.querySelector('.video-player')===window.mediaWrapper};})()`);
    report.scenes.push({ label, rgb, bounds, captureSize: size, sampledPixel: { x, y }, ...snapshot });
    assert(rgb[channel] > 75 && rgb.every((value, index) => index === channel || rgb[channel] > value + 35),
      `Wrong actual decoded scene for ${label}: ${JSON.stringify({ rgb, snapshot })}`);
    return snapshot;
  }
  function check(name) { report.checks[name] = true; console.log(JSON.stringify({ check: name, passed: true })); }

  try {
    await window.webContents.session.clearStorageData({ storages: ['localstorage'] });
    await window.loadFile(path.join(root, 'ui', 'index.html'));
    await evaluate(`document.addEventListener('loadstart', event => {
      const video=event.target; if (!(video instanceof HTMLVideoElement)) return;
      if (video.fixturePresentedFrame?.callback) video.cancelVideoFrameCallback(video.fixturePresentedFrame.callback);
      const frame={src:null,mediaTime:null,presentedFrames:0,callback:0}; video.fixturePresentedFrame=frame;
      const presented=(_now,metadata) => {
        if (video.fixturePresentedFrame!==frame) return;
        frame.src=video.currentSrc; frame.mediaTime=metadata.mediaTime; frame.presentedFrames=metadata.presentedFrames;
        frame.callback=video.requestVideoFrameCallback(presented);
      };
      frame.callback=video.requestVideoFrameCallback(presented);
    }, true)`);
    await until(`typeof window.bridge.fixtureUseTranscodedStreams==='function' && window.bridge.fixtureCalls().some(c=>c.command==='state')`);
    await until(`window.bridge.fixtureCalls().some(c=>c.command==='scan'&&c.args.source==='photos')`);
    await evaluate('window.bridge.fixtureResolvePhotoScan(); window.bridge.fixturePlayableStream(); window.bridge.fixtureUseTranscodedStreams(true)');
    await click('[data-view="apps"]');
    await click('[data-app-id="blackmagic"]');
    await until('document.querySelector(\'[data-key="folder-media"]\')');
    await evaluate(`document.querySelector('[data-key="folder-media"]').dispatchEvent(new MouseEvent('dblclick',{bubbles:true}))`);
    await click('[data-key="fixture-local-video"]');
    await evaluate(`window.bridge.fixtureDurationEvent('fixture-local-video',12)`);
    await until(`document.querySelector('#inspector-content [data-video-duration] dd')?.textContent==='0:12'`);
    await click('[data-inspector-action="compatible"]');
    await ready(0);
    await evaluate(`(() => {const v=document.querySelector('#inspector-content video');v.pause();v.volume=.37;v.playbackRate=1.25;
      window.mediaVideo=v;window.mediaWrapper=document.querySelector('.video-player');})()`);
    await scene('compatible beginning', 0);
    assert.equal(await evaluate('document.querySelectorAll("#video-position").length'), 1);
    assert.equal(await evaluate(`document.querySelector('.video-player').contains(document.querySelector('#video-position'))`), true);
    assert.equal(await evaluate(`Array.from(document.styleSheets).some(sheet=>Array.from(sheet.cssRules).some(rule=>rule.selectorText?.includes('video::-webkit-media-controls-timeline')&&rule.style.display==='none'))`), true);
    check('oneAuthoritativeTimelineWithNativeTimelineHidden');

    await seek(6.3); await ready(6.3);
    const green = await scene('paused seek to green at6.3', 1);
    assert.equal(green.paused, true);
    assert(Math.abs(green.volume - .37) < .01 && Math.abs(green.rate - 1.25) < .01);
    assert(green.sameVideo && green.sameWrapper);
    check('actualCompatibleGreenFrameAtRequestedOffset');
    check('pausedStateVolumeAndRateSurviveSourceReload');
    check('videoAndWrapperRetained');

    await evaluate(`document.querySelector('[data-inspector-action="fullscreen"]').click()`, true);
    await until(`document.fullscreenElement===window.mediaWrapper && document.fullscreenElement.contains(document.querySelector('#video-position'))`);
    await seek(10.3); await ready(10.3);
    const blue = await scene('fullscreen paused seek to blue at10.3', 2);
    assert(blue.sameVideo && blue.sameWrapper && blue.paused);
    assert.equal(await evaluate('document.fullscreenElement===window.mediaWrapper'), true);
    check('fullscreenWrapperAndTimelineSurviveSeek');
    check('actualCompatibleBlueFrameAtRequestedOffset');
    await evaluate('document.exitFullscreen()', true);
    await until('!document.fullscreenElement');

    await evaluate(`window.bridge.fixtureDelayMedia('preview')`);
    const before = (await calls()).filter(c => c.command === 'preview' && c.args.compatible).length;
    await seek(7.1);
    await until(`window.bridge.fixtureCalls().filter(c=>c.command==='preview'&&c.args.compatible).length>${before}`);
    await evaluate(`(() => {const v=document.querySelector('#inspector-content video');v.dispatchEvent(new Event('timeupdate'));})()`);
    assert.equal(await evaluate('Number(document.querySelector("#video-position").value)'), 7.1);
    check('oldTimeupdateCannotResetPendingTarget');
    await seek(10.3); await ready(10.3);
    const latest = await evaluate('document.querySelector("#inspector-content video").currentSrc');
    await evaluate('window.bridge.fixtureResolveMedia()');
    await until(`window.bridge.fixtureCalls().some(c=>c.command==='stop_stream'&&c.args.url.includes('fixture-transcoded-7100-'))`);
    assert.equal(await evaluate('document.querySelector("#inspector-content video").currentSrc'), latest);
    await scene('rapid seek latest blue survives delayed green response', 2);
    check('lateOldSeekResponseCannotReplaceLatestMedia');

    await evaluate('document.querySelector("#inspector-content video").play()', true);
    await until('document.querySelector("#inspector-content video").paused===false');
    await seek(6.3);
    await until(`document.querySelector('#inspector-content video')?.readyState>=2 && !document.querySelector('#inspector-content video').paused && document.querySelector('#inspector-content video').currentSrc.includes('fixture-transcoded-6300-')`);
    const playing = await scene('playing seek resumes at green', 1);
    assert(playing.sameVideo && playing.sameWrapper && !playing.paused);
    check('playingStateSurvivesSeek');
    await evaluate('document.querySelector("#inspector-content video").pause()');

    const nativeBefore = (await calls()).filter(c => c.command === 'preview' && c.args.compatible).length;
    const originalCallsBefore = (await calls()).length;
    await click('[data-inspector-action="preview"]');
    await until(`document.querySelector('#inspector-content video')?.readyState>=2 && !document.querySelector('#inspector-content video').paused && document.querySelector('#inspector-content video').currentSrc.includes('fixture-playable-')`);
    const localOriginalCalls = (await calls()).slice(originalCallsBefore).filter(call => ['stream','preview'].includes(call.command));
    assert.equal(localOriginalCalls.length, 1);
    assert.equal(localOriginalCalls[0].command, 'stream');
    assert.equal(localOriginalCalls[0].args.compatible, false);
    assert.equal(localOriginalCalls[0].args.preferLocal, true);
    assert.equal(await evaluate(`document.querySelector('.stream-status')?.textContent.includes('Downloaded original on this PC')`), true);
    await evaluate(`document.querySelector('#inspector-content video').pause();window.mediaVideo=document.querySelector('#inspector-content video');window.mediaWrapper=document.querySelector('.video-player')`);
    await scene('explicit local original starts with actual red frame', 0);
    check('explicitPlayDownloadedOriginalAutoplaysSeekableLocalStream');
    await seek(10.3);
    await until(`(() => {const v=document.querySelector('#inspector-content video');return !v.seeking&&Math.abs(v.currentTime-10.3)<.1;})()`);
    await scene('original byte-range seek to blue', 2);
    assert.equal((await calls()).filter(c => c.command === 'preview' && c.args.compatible).length, nativeBefore);
    check('nativeOriginalSeekDoesNotRestartCompatibleSession');

    await evaluate('document.querySelector("#inspector-content video").play()', true);
    await until('document.querySelector("#inspector-content video").ended===true', 8000);
    const endedCompatibleBefore = (await calls()).filter(c => c.command === 'preview' && c.args.compatible).length;
    await seek(6.3);
    await until(`(() => {const v=document.querySelector('#inspector-content video');return !v.seeking&&v.paused&&!v.ended&&Math.abs(v.currentTime-6.3)<.1;})()`);
    const afterEnded = await scene('original ended then backward while paused', 1);
    assert(afterEnded.paused && afterEnded.sameVideo);
    assert.equal((await calls()).filter(c => c.command === 'preview' && c.args.compatible).length, endedCompatibleBefore);
    await evaluate('document.querySelector("#inspector-content video").play()', true);
    await until('document.querySelector("#inspector-content video").paused===false');
    await evaluate('document.querySelector("#inspector-content video").pause()');
    check('nativeOriginalBackwardSeekAfterEndedLoadsPausedFrameAndCanPlay');

    await click('[data-inspector-action="compatible"]');
    await ready(0);
    await evaluate('document.querySelector("#inspector-content video").pause()');
    await seek(10.3); await ready(10.3);
    await evaluate('document.querySelector("#inspector-content video").play()', true);
    await until('document.querySelector("#inspector-content video").ended===true', 8000);
    await seek(6.3); await ready(6.3);
    const compatibleAfterEnded = await scene('compatible ended then backward while paused', 1);
    assert(compatibleAfterEnded.paused && compatibleAfterEnded.sameVideo && compatibleAfterEnded.sameWrapper);
    await evaluate('document.querySelector("#inspector-content video").play()', true);
    await until('document.querySelector("#inspector-content video").paused===false');
    await evaluate('document.querySelector("#inspector-content video").pause()');
    check('compatibleBackwardSeekAfterEndedLoadsPausedFrameAndCanPlay');

    // Disconnect only the fixture UI state. The catalogue retains the verified
    // localPath, and all playback must keep selecting the PC route while offline.
    await evaluate(`(async()=>{const state=await window.bridge.request('state');
      window.bridge.fixtureEmit({event:'state',data:{
        devices:state.devices.map(device=>({...device,online:false})),
        connection:{status:'offline',serviceAvailable:false}}});})()`);
    await until(`document.body.innerText.includes('Saved device · offline')`);
    await until(`document.querySelector('[data-inspector-action="stream"]')?.disabled===true`);
    assert.equal(await evaluate(`document.querySelector('[data-inspector-action="preview"]')?.disabled`), false);
    assert.equal(await evaluate(`document.querySelector('[data-inspector-action="compatible"]')?.disabled`), false);
    check('mountedOfflinePlayerDisablesDeviceStreamButKeepsLocalPlaybackAvailable');
    const offlineCallsBefore = (await calls()).length;
    await click('[data-inspector-action="preview"]');
    await until(`document.querySelector('#inspector-content video')?.readyState>=2 && !document.querySelector('#inspector-content video').paused && document.querySelector('#inspector-content video').currentSrc.includes('fixture-playable-')`);
    await evaluate(`document.querySelector('#inspector-content video').pause();window.mediaVideo=document.querySelector('#inspector-content video');window.mediaWrapper=document.querySelector('.video-player')`);
    await scene('offline local original starts red', 0);
    await seek(10.3);
    await until(`(()=>{const video=document.querySelector('#inspector-content video');return !video.seeking&&video.paused&&Math.abs(video.currentTime-10.3)<.1;})()`);
    await scene('offline original native seek forward to blue', 2);
    await seek(6.3);
    await until(`(()=>{const video=document.querySelector('#inspector-content video');return !video.seeking&&video.paused&&Math.abs(video.currentTime-6.3)<.1;})()`);
    const offlineGreen = await scene('offline original native seek backward to green', 1);
    assert(offlineGreen.sameVideo && offlineGreen.sameWrapper && offlineGreen.paused);
    check('offlineDownloadedOriginalNativeForwardAndBackwardSeekDecodeRequestedFrames');

    // Simulate an unsupported original codec only after real local original
    // playback succeeds. The error handler must request a compatible *local*
    // session at the same original time without needing an online device.
    const fallbackCallsBefore = (await calls()).length;
    await evaluate(`document.querySelector('#inspector-content video').dispatchEvent(new Event('error'))`);
    await ready(6.3);
    const automaticFallback = (await calls()).slice(fallbackCallsBefore).find(call=>call.command==='preview'&&call.args.compatible);
    assert(automaticFallback);
    assert(Math.abs(automaticFallback.args.start-6.3)<.1);
    assert(automaticFallback.args.item.localPath);
    const offlineFallback = await scene('offline automatic compatible fallback keeps green original time', 1);
    assert(offlineFallback.paused && offlineFallback.sameVideo && offlineFallback.sameWrapper);
    await seek(10.3); await ready(10.3);
    await scene('offline automatic compatible fallback seeks forward to blue', 2);
    await seek(6.3); await ready(6.3);
    await scene('offline automatic compatible fallback seeks backward to green', 1);
    await evaluate('document.querySelector("#inspector-content video").play()', true);
    await until('document.querySelector("#inspector-content video").paused===false');
    await evaluate('document.querySelector("#inspector-content video").pause()');
    const offlineCalls = (await calls()).slice(offlineCallsBefore).filter(call=>['preview','stream'].includes(call.command));
    assert(offlineCalls.some(call=>call.command==='stream'&&call.args.preferLocal===true&&!call.args.compatible));
    assert.equal(offlineCalls.some(call=>call.command==='stream'&&call.args.preferLocal!==true),false);
    assert(offlineCalls.filter(call=>call.command==='preview').every(call=>call.args.compatible&&call.args.item.localPath));
    report.offlinePlaybackCalls=offlineCalls;
    check('unsupportedOfflineOriginalAutomaticallyFallsBackToLocalCompatibleWithoutDeviceAccess');
    assert(requests.some(request=>request.kind==='original'&&request.status===206));
    check('originalPlaybackUsesActualByteRangeResponses');

    assert.equal((await calls()).some(c => ['download', 'upload', 'confirm_phone_delete', 'confirm_pc_delete'].includes(c.command)), false);
    assert(requests.filter(r => r.kind === 'compatible').every(r => r.status === 200 && !r.byteRangeSupported));
    check('noOriginalTransfersOrDeviceMutations');
    report.requests = requests;
    report.consoleErrors = consoleErrors;
    report.passed = true;
    fs.writeFileSync(reportFile, JSON.stringify(report, null, 2));
    console.log(JSON.stringify({ passed: true, report: reportFile, checks: report.checks }));
  } catch (error) {
    report.error = error.stack || String(error);
    report.requests = requests;
    report.consoleErrors = consoleErrors;
    try { report.dom = await evaluate('document.body.innerText'); } catch {}
    try { report.player = await evaluate(`(() => {const v=document.querySelector('#inspector-content video');return v?{src:v.src,currentSrc:v.currentSrc,time:v.currentTime,duration:v.duration,ready:v.readyState,paused:v.paused,ended:v.ended,rate:v.playbackRate,volume:v.volume,timeline:document.querySelector('#video-position')?.value}:null})()`); } catch {}
    try { fs.writeFileSync(path.join(fixture, 'failure.png'), (await window.webContents.capturePage(undefined, { stayHidden: true })).toPNG()); } catch {}
    fs.writeFileSync(reportFile, JSON.stringify(report, null, 2));
    console.error(report.error);
  } finally {
    window.destroy();
    app.exit(report.passed ? 0 : 1);
  }
}).catch(error => { console.error(error); app.exit(1); });
