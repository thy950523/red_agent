const assert = require('node:assert/strict');
const path = require('node:path');
const widgetRoot = require('./widget-path.cjs');

const configPath = path.join(widgetRoot, 'utils/match-config.js');
require.cache[require.resolve(configPath)] = {
  id: configPath,
  filename: configPath,
  loaded: true,
  exports: { MATCH_API_URL: '' }
};

let page;
let chooseOptions;
global.Page = definition => {
  page = definition;
  page.data = { ...definition.data };
  page.setData = update => Object.assign(page.data, update);
};
global.xhs = {
  chooseMedia(options) {
    chooseOptions = options;
    options.success({ tempFiles: [{ tempFilePath: '/tmp/portrait.png' }] });
  },
  showToast() {},
  uploadFile() { throw new Error('must not upload without configured detection service'); }
};

require(path.join(widgetRoot, 'pages/index/index.js'));
page.onLoad();

assert.equal(chooseOptions.count, 1);
assert.deepEqual(chooseOptions.sourceType, ['album']);
assert.deepEqual(chooseOptions.sizeType, ['compressed']);
assert.equal(page.data.entered, false);
assert.equal(page.data.fishId, '');
assert.match(page.data.error, /人脸检测服务/);
page.generateImage();
assert.equal(page.data.generatedImage, '');
console.log('widget blocks matching until face detection is configured');
