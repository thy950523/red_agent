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
let upload;
let tick;
let cleared = 0;
let chooseCalls = 0;
global.getApp = () => ({ ensureLogin: callback => callback(null, 'test-token', null, 'test-open-id'), globalData: {} });
global.Page = definition => {
  page = definition;
  page.data = { ...definition.data };
  page.setData = update => Object.assign(page.data, update);
};
global.setInterval = callback => { tick = callback; return 1; };
global.clearInterval = () => { cleared += 1; tick = null; };
global.xhs = {
  uploadFile(options) { upload = options; },
  chooseMedia() { chooseCalls += 1; },
  showToast() {}
};

require(path.join(widgetRoot, 'pages/index/index.js'));
page.data.fishId = '22';
page.data.imageUrl = '/tmp/portrait.png';

page.generateImage();
assert.equal(page.data.generating, true);
assert.equal(page.data.generationProgress, 8);
page.chooseImage();
assert.equal(chooseCalls, 0);
for (let i = 0; i < 150; i += 1) tick();
assert.equal(page.data.generationProgress, 92);
upload.fail();
assert.equal(page.data.generating, false);
assert.equal(tick, null);

page.generateImage();
assert.equal(page.data.generationProgress, 8);
upload.success({
  statusCode: 200,
  data: JSON.stringify({ imageUrl: 'https://api.example.test/result/abc.png' })
});
assert.equal(page.data.generating, false);
assert.equal(page.data.generationProgress, 100);
assert.equal(page.data.generatedImage, 'https://api.example.test/result/abc.png');
assert.equal(tick, null);
assert.equal(cleared, 2);
console.log('generation progress stops on failure and success');
