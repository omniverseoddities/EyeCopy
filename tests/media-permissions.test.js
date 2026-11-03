'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { installMediaPermissions } = require('../desktop/media-permissions');

function fixture() {
  let check, request;
  const url = 'file:///fixture/ui/index.html';
  const contents = { getURL: () => url, isDestroyed: () => false,
    session: { setPermissionCheckHandler: value => { check = value; }, setPermissionRequestHandler: value => { request = value; } } };
  installMediaPermissions({ webContents: contents }, url);
  const details = { requestingUrl: url, isMainFrame: true };
  return { contents, details, check: (p, c=contents, d=details) => check(c,p,'file://',d),
    request: (p, c=contents, d=details) => { let result; request(c,p,value=>{result=value;},d); return result; } };
}

test('Only the owned top-level EyeCopy page can enter user-initiated fullscreen', () => {
  const f=fixture(); assert.equal(f.check('fullscreen'),true); assert.equal(f.request('fullscreen'),true);
  assert.equal(f.check('fullscreen',null),false);
  assert.equal(f.request('fullscreen',{...f.contents}),false);
  assert.equal(f.check('fullscreen',f.contents,{...f.details,isMainFrame:false}),false);
  assert.equal(f.request('fullscreen',f.contents,{...f.details,requestingUrl:'file:///fixture/other.html'}),false);
  assert.equal(f.check('fullscreen',f.contents,{}),false);
});
test('Unrelated permissions and automatic fullscreen remain denied', () => {
  const f=fixture();
  for(const permission of ['automatic-fullscreen','media','geolocation','notifications','clipboard-read','openExternal','usb','unknown']) {
    assert.equal(f.check(permission),false,permission); assert.equal(f.request(permission),false,permission);
  }
  f.contents.isDestroyed=()=>true; assert.equal(f.check('fullscreen'),false);
});
