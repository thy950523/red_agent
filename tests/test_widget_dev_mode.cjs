const assert = require('node:assert/strict');
const path = require('node:path');
const widgetRoot = require('./widget-path.cjs');

const configPath = path.join(widgetRoot, 'utils/match-config.js');
require.cache[require.resolve(configPath)] = {
  id: configPath, filename: configPath, loaded: true,
  exports: { MATCH_API_URL: 'http://127.0.0.1:8765/match' }
};

let app;
let page;
let configRequest;
let upload;
let loginCalls = 0;
global.App = definition => { app = definition; };
global.getApp = () => app;
global.Page = definition => {
  page = definition;
  page.data = { ...definition.data };
  page.setData = update => Object.assign(page.data, update);
};
global.xhs = {
  login() { loginCalls += 1; },
  request(options) { configRequest = options; },
  uploadFile(options) { upload = options; },
  showToast() {}
};

require(path.join(widgetRoot, 'app.js'));
require(path.join(widgetRoot, 'pages/index/index.js'));
app.onLaunch();
assert.equal(configRequest.url, 'http://127.0.0.1:8765/auth/config');
configRequest.success({ statusCode: 200, data: { authEnabled: false } });
assert.equal(loginCalls, 0);

page.matchWithService('/tmp/portrait.png');
assert.equal(upload.url, 'http://127.0.0.1:8765/match');
assert.deepEqual(upload.formData, {});
assert.deepEqual(upload.header, {});

page.data.fishId = '22';
page.data.imageUrl = '/tmp/portrait.png';
page.generateImage();
assert.equal(upload.url, 'http://127.0.0.1:8765/generate');
assert.deepEqual(upload.formData, { fishId: '22' });
assert.deepEqual(upload.header, {});
assert.equal(loginCalls, 0);
upload.fail();
console.log('local mode skips platform login and uploads without auth');
