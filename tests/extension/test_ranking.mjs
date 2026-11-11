import test from 'node:test';
import assert from 'node:assert/strict';
import {allowedRankingPage, rankingDom, rankingOperation} from '../../extension/ranking.mjs';

import {apiFixture} from './helpers/ranking-fixture.mjs';

test('standalone collection creates and closes its own window tab', async () => {
  const {api, tabs} = apiFixture();
  tabs.set(77, {id: 77, windowId: 1, url: 'https://example.com'});
  let opened;
  api.windows.create = async params => {
    opened = params;
    return {tabs: [await api.tabs.create({url: params.url, windowId: 2})]};
  };
  const {tab_id} = await rankingOperation(api, 'ranking_open', {new_window: true});
  assert.deepEqual(opened, {url: 'about:blank', focused: true, type: 'normal'});
  assert.equal(tabs.get(tab_id).windowId, 2);
  await rankingOperation(api, 'ranking_close', {tab_id});
  assert.equal(tabs.has(tab_id), false);
  assert.equal(tabs.has(77), true);
});

test('navigation waits for the new page to finish loading before DOM collection', async () => {
  const {api, tabs, calls} = apiFixture();
  const {tab_id} = await rankingOperation(api, 'ranking_open', {});
  const update = api.tabs.update;
  api.tabs.update = async (id, params) => {
    const tab = await update(id, params);
    tab.status = 'loading';
    return tab;
  };
  const get = api.tabs.get;
  let reads = 0;
  api.tabs.get = async id => {
    const tab = await get(id);
    if (tab.status === 'loading' && ++reads === 3) tab.status = 'complete';
    return tab;
  };
  await rankingOperation(api, 'ranking_navigate', {tab_id, url: 'https://mobile.yangkeduo.com/rank'});
  assert.equal(reads, 3);
  assert.equal(calls.some(call => call[0] === 'dom'), false);
  await rankingOperation(api, 'ranking_read', {tab_id, load: true});
  assert.equal(tabs.get(tab_id).status, 'complete');
});

test('ranking page scope accepts mobile PDD and refuses seller pages and foreign hosts', () => {
  assert.equal(allowedRankingPage('https://mobile.yangkeduo.com/goods_ranking.html'), true);
  assert.equal(allowedRankingPage('https://mobile.pinduoduo.com/rank'), true);
  assert.equal(allowedRankingPage('http://127.0.0.1:1234/extension-test/ranking/fixture'), true);
  for (const url of ['https://mms.pinduoduo.com/home', 'https://mobile.yangkeduo.com.other.example/rank',
    'https://user:pass@mobile.yangkeduo.com/rank', 'http://mobile.yangkeduo.com/rank', 'https://example.com']) {
    assert.equal(allowedRankingPage(url), false);
  }
});

test('opening a task preserves manual window size, zoom, and viewport', async () => {
  const {api, state, calls} = apiFixture();
  const {tab_id} = await rankingOperation(api, 'ranking_open', {});
  assert.equal(calls.filter(call => call[0].startsWith('Emulation.') || ['zoom','zoomSettings','window'].includes(call[0])).length, 0);
  assert.ok(state.rankingTabs[tab_id]);
  await rankingOperation(api, 'ranking_close', {tab_id});
  assert.deepEqual(state.rankingTabs, {});
});

test('read and screenshot are performed inside the registered extension tab and preserve manual viewport', async () => {
  const {api, calls} = apiFixture();
  const {tab_id} = await rankingOperation(api, 'ranking_open', {});
  await rankingOperation(api, 'ranking_navigate', {tab_id, url: 'https://mobile.yangkeduo.com/rank'});
  assert.equal((await rankingOperation(api, 'ranking_read', {tab_id, load: true})).cards[0].goods_id, '123');
  const shot = await rankingOperation(api, 'ranking_screenshot', {tab_id, goods_id: '123'});
  assert.equal(shot.format, 'jpeg');
  assert.deepEqual(calls.find(call => call[0] === 'Page.captureScreenshot')[2].clip,
    {x: 0, y: 120, width: 390, height: 844, scale: 1});
  assert.equal(calls.filter(call => call[0] === 'attach').length, 1);
  assert.equal(calls.filter(call => call[0] === 'detach').length, 0);
  assert.deepEqual(calls.filter(call => call[0] === 'dom').at(-1)[1].args, [{goods_id: '123'}]);
});

test('does not operate on unregistered tabs or navigate outside ranking scope', async () => {
  const {api, calls, tabs} = apiFixture();
  tabs.set(77, {id: 77, url: 'https://mobile.yangkeduo.com/rank'});
  await assert.rejects(rankingOperation(api, 'ranking_close', {tab_id: 77}), /not_owned/);
  const {tab_id} = await rankingOperation(api, 'ranking_open', {});
  await assert.rejects(rankingOperation(api, 'ranking_navigate', {tab_id, url: 'https://example.com'}), /page_not_allowed/);
  assert.equal(calls.filter(call => call[0] === 'update').length, 0);
  assert.ok(tabs.has(77));
});

test('supports an owned blank tab whose URL is hidden without the tabs permission', async () => {
  const {api, tabs} = apiFixture();
  const {tab_id} = await rankingOperation(api, 'ranking_open', {});
  delete tabs.get(tab_id).url;
  await rankingOperation(api, 'ranking_navigate', {tab_id, url: 'https://mobile.yangkeduo.com/rank'});
  assert.equal(tabs.get(tab_id).url, 'https://mobile.yangkeduo.com/rank');
  delete tabs.get(tab_id).url; // An inaccessible redirect is not treated as a new blank tab.
  await assert.rejects(rankingOperation(api, 'ranking_read', {tab_id}), /page_not_allowed/);
});

test('reattaches debugger after a debugger detach and cleans failed setup', async () => {
  const {api, calls, attached, state, tabs} = apiFixture();
  const {tab_id} = await rankingOperation(api, 'ranking_open', {});
  await rankingOperation(api, 'ranking_navigate', {tab_id, url: 'https://mobile.yangkeduo.com/rank'});
  attached.delete(tab_id);
  await rankingOperation(api, 'ranking_read', {tab_id});
  assert.equal(calls.filter(call => call[0] === 'attach').length, 2);
  const original = api.debugger.sendCommand;
  api.debugger.sendCommand = async (target, command, params) => {
    if (command === 'Page.getLayoutMetrics') throw new Error('unsupported');
    return original(target, command, params);
  };
  api.debugger.attach = async () => { throw new Error('unsupported'); };
  await assert.rejects(rankingOperation(api, 'ranking_open', {}), /unsupported/);
  assert.equal(tabs.size, 1);
  assert.deepEqual(Object.keys(state.rankingTabs), [String(tab_id)]);
});

test('DOM extraction preserves original card text and attributes without executing caller code', async () => {
  const card = {
    innerText: '商品标题 ￥12.30 热卖指数123 疯抢2万件',
    getAttribute: () => '123',
    querySelector: selector => selector === '[title]' ? {getAttribute: () => '商品标题'} : null,
    querySelectorAll: () => [{getAttribute: () => 'https://images.example/2024-04-27/image.jpg'}]
  };
  globalThis.location = {href: 'https://mobile.yangkeduo.com/rank'};
  globalThis.innerWidth = 390;
  globalThis.innerHeight = 844;
  globalThis.XPathResult = {FIRST_ORDERED_NODE_TYPE: 9};
  globalThis.document = {
    querySelectorAll: selector => selector === '.Bt00keaq' ? [{innerText: '畅销榜'}] : [card, card],
    evaluate: () => ({singleNodeValue: {textContent: '猫零食畅销榜'}})
  };
  try {
    const result = await rankingDom();
    assert.equal(result.cards.length, 1);
    assert.equal(result.cards[0].price, '12.30');
    assert.equal(result.cards[0].listing, '2024-04-27');
    assert.equal(result.cards[0].title, '商品标题');
    assert.equal(result.cards[0].text, card.innerText);
    assert.deepEqual(result.tabs, ['畅销榜']);
    globalThis.location.href = 'https://example.com';
    await assert.rejects(rankingDom(), /page_not_allowed/);
  } finally {
    for (const key of ['location', 'innerWidth', 'innerHeight', 'XPathResult', 'document']) delete globalThis[key];
  }
});
