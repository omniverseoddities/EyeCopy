'use strict';

function installMediaPermissions(window, uiURL) {
  const trusted = window.webContents;
  const allowed = (contents, permission, details) => permission === 'fullscreen'
    && contents === trusted && !contents.isDestroyed() && contents.getURL() === uiURL
    && details?.isMainFrame === true && details.requestingUrl === uiURL;
  trusted.session.setPermissionCheckHandler((contents, permission, _origin, details) => allowed(contents, permission, details));
  trusted.session.setPermissionRequestHandler((contents, permission, callback, details) => callback(allowed(contents, permission, details)));
}
module.exports = { installMediaPermissions };
