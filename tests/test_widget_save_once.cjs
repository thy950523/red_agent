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
let download;
let save;
let downloadCalls = 0;
let saveCalls = 0;
global.Page = definition => {
  page = definition;
  page.data = { ...definition.data };
  page.setData = update => Object.assign(page.data, update);
};
global.xhs = {
  showToast() {},
  downloadFile(options) { downloadCalls += 1; download = options; },
  saveImageToPhotosAlbum(options) { saveCalls += 1; save = options; }
};

require(path.join(widgetRoot, 'pages/index/index.js'));
page.data.generatedImage = 'https://api.example.test/result/one.png';
page.saveGeneratedImage();
page.saveGeneratedImage();
assert.equal(downloadCalls, 1);
assert.equal(page.data.savingImage, true);

download.success({ tempFilePath: '/tmp/one.png' });
page.saveGeneratedImage();
assert.equal(saveCalls, 1);
save.success();
assert.equal(page.data.savingImage, false);
assert.equal(page.data.imageSaved, true);
page.saveGeneratedImage();
assert.equal(downloadCalls, 1);

page.showMatch('23');
assert.equal(page.data.imageSaved, false);
page.data.generatedImage = 'https://api.example.test/result/two.png';
page.saveGeneratedImage();
download.fail();
assert.equal(page.data.savingImage, false);
assert.equal(page.data.imageSaved, false);
page.saveGeneratedImage();
assert.equal(downloadCalls, 3);
console.log('save runs once after success and can retry after failure');
