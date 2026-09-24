const assert = require('node:assert/strict');
const path = require('node:path');
const widgetRoot = require('./widget-path.cjs');

const configPath = path.join(widgetRoot, 'utils/match-config.js');
require.cache[require.resolve(configPath)] = {
  id: configPath,
  filename: configPath,
  loaded: true,
  exports: { MATCH_API_URL: 'https://api.example.test/match' }
};

let page;
const responses = [
  { statusCode: 422, data: JSON.stringify({ detail: { code: 'NO_FACE', message: '请上传一张包含人脸的图片' } }) },
  { statusCode: 413, data: JSON.stringify({ detail: '照片不能超过 20 MB' }) },
  { statusCode: 200, data: JSON.stringify({ match: { id: '22' } }) }
];
global.Page = definition => {
  page = definition;
  page.data = { ...definition.data };
  page.setData = update => Object.assign(page.data, update);
};
global.xhs = {
  chooseMedia: ({ success }) => success({ tempFiles: [{ tempFilePath: '/tmp/portrait.png' }] }),
  uploadFile: ({ success }) => success(responses.shift()),
  showToast() {}
};
require(path.join(widgetRoot, 'pages/index/index.js'));

page.onLoad();
assert.equal(page.data.entered, false);
assert.equal(page.data.error, '请上传一张包含人脸的图片');

page.chooseImage();
assert.equal(page.data.entered, false);
assert.equal(page.data.error, '照片不能超过 20 MB');

page.chooseImage();
assert.equal(page.data.entered, true);
assert.equal(page.data.fishId, '22');
assert.equal(page.data.error, '');
console.log('widget shows no-face and size errors, then accepts a match');
