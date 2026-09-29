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
let published;
let uploadedTo;
global.getApp = () => ({ ensureLogin: callback => callback(null, 'test-token', null, 'test-open-id'), globalData: {} });
global.Page = definition => {
  page = definition;
  page.data = { ...definition.data };
  page.setData = update => Object.assign(page.data, update);
};
global.xhs = {
  showToast() {},
  uploadFile(options) {
    uploadedTo = options.url;
    options.success({
      statusCode: 200,
      data: JSON.stringify({ imageUrl: 'https://api.example.test/result/abc.png' })
    });
  },
  postNote(options) { published = options; }
};

require(path.join(widgetRoot, 'pages/index/index.js'));
page.data.fishId = '22';
page.data.imageUrl = '/tmp/portrait.png';
page.publishNote();

assert.equal(uploadedTo, undefined);
assert.equal(published, undefined);
page.generateImage();
assert.equal(page.data.generatedImage, 'https://api.example.test/result/abc.png');
page.publishNote();

assert.equal(uploadedTo, 'https://api.example.test/generate');
assert.equal(JSON.parse(published.mediaInfo).image_resources[0].url, 'https://api.example.test/result/abc.png');
console.log('publish opens the editor only after image generation');
