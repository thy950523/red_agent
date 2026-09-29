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
let published;
let lastToast;
global.getApp = () => ({
  globalData: { postNote: { title: '我是路人鱼', content: '来看看你像谁', tags: '路人鱼' } },
  fetchPostNote(callback) { callback(this.globalData.postNote); }
});
global.Page = definition => {
  page = definition;
  page.data = { ...definition.data };
  page.setData = update => Object.assign(page.data, update);
};
global.xhs = {
  showLoading() {},
  hideLoading() {},
  showToast(options) { lastToast = options.title; },
  uploadFile() { throw new Error('publish must reuse generated URL, not upload again'); },
  postNote(options) { published = options; }
};
require(path.join(widgetRoot, 'pages/index/index.js'));

page.data.generatedImage = 'https://example.test/result/abc.png';
page.publishNote();

assert.equal(JSON.parse(published.mediaInfo).image_resources[0].url, page.data.generatedImage);
assert.equal(published.title, '我是路人鱼');
assert.equal(published.content, '来看看你像谁');
assert.equal(published.tags, '路人鱼');
page.data.generatedImage = '';
page.data.imageUrl = '/tmp/portrait.png';
published = null;
page.publishNote();
assert.equal(published, null);
assert.equal(lastToast, undefined);
console.log('publish uses generated image URL and backend-provided note fields');
