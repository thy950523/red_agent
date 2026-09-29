const assert = require('node:assert/strict');
const path = require('node:path');
const widgetRoot = require('./widget-path.cjs');

const configPath = path.join(widgetRoot, 'utils/match-config.js');
require.cache[require.resolve(configPath)] = {
  id: configPath, filename: configPath, loaded: true,
  exports: { MATCH_API_URL: 'https://api.example.test/match' }
};

let app;
let page;
let loginCalls = 0;
let exchange;
let upload;
global.App = definition => { app = definition; };
global.getApp = () => app;
global.Page = definition => {
  page = definition;
  page.data = { ...definition.data };
  page.setData = update => Object.assign(page.data, update);
};
global.xhs = {
  login({ success }) { loginCalls += 1; success({ code: 'single-use-code' }); },
  request(options) { exchange = options; },
  uploadFile(options) { upload = options; },
  showToast() {}
};

require(path.join(widgetRoot, 'app.js'));
require(path.join(widgetRoot, 'pages/index/index.js'));
app.onLaunch();
app.ensureLogin(() => {});
assert.equal(exchange.url, 'https://api.example.test/auth/config');
assert.equal(loginCalls, 0);
exchange.success({ statusCode: 200, data: { authEnabled: true } });
assert.equal(loginCalls, 1);
assert.equal(exchange.url, 'https://api.example.test/auth/xhs');
assert.deepEqual(exchange.data, { code: 'single-use-code' });
exchange.success({ statusCode: 200, data: { token: 'server-token', openid: 'verified-open-id', quota: { remaining: 5 } } });

page.matchWithService('/tmp/portrait.png');
assert.equal(upload.url, 'https://api.example.test/match');
assert.equal(upload.header.Authorization, 'Bearer server-token');
assert.equal(upload.formData.openid, 'verified-open-id');

page.data.fishId = '22';
page.data.imageUrl = '/tmp/portrait.png';
page.generateImage();
assert.equal(upload.header.Authorization, 'Bearer server-token');
assert.equal(upload.formData.openid, 'verified-open-id');
assert.equal(upload.url, 'https://api.example.test/generate');
upload.success({
  statusCode: 200,
  data: JSON.stringify({ imageUrl: 'https://api.example.test/result/one.png', quota: { remaining: 4 } })
});
assert.equal(page.data.quotaRemaining, 4);
assert.equal(app.globalData.quota.remaining, 4);
console.log('widget exchanges login code and sends server token when generating');
