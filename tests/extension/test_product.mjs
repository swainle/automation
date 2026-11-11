import test from 'node:test';
import assert from 'node:assert/strict';
import {productOperation, productNetworkEvent, productDom} from '../../extension/product.mjs';
import {rankingOperation} from '../../extension/ranking.mjs';
import {apiFixture} from './helpers/ranking-fixture.mjs';

test('prepared Sogou workspace is reused without changing manual browser settings', async () => {
  const {api, tabs, calls} = apiFixture();
  const first = await rankingOperation(api, 'ranking_prepare', {width: 390, height: 844, scale: .5});
  const second = await rankingOperation(api, 'ranking_open', {reuse_workspace: true, width: 375, height: 812, scale: .5});
  assert.equal(second.tab_id, first.tab_id);
  assert.equal(second.persistent, true);
  assert.equal(second.width, undefined);
  assert.equal(tabs.size, 1);
  assert.equal(calls.filter(call => ['window','zoom','zoomSettings'].includes(call[0]) || call[0].startsWith('Emulation.')).length, 0);
});

test('product captures network before navigation, isolates review phases, and preserves prepared browser', async () => {
  const {api, calls, tabs} = apiFixture();
  const workspace = await rankingOperation(api, 'ranking_prepare', {});
  const bodies = new Map();
  const send = api.debugger.sendCommand;
  api.debugger.sendCommand = async (target, command, params) => {
    if (command === 'Network.getResponseBody') return {body: JSON.stringify(bodies.get(params.requestId)), base64Encoded: false};
    return send(target, command, params);
  };
  const opened = await productOperation(api, 'product_open', {url: 'https://mobile.yangkeduo.com/goods.html?goods_id=123'});
  const tab_id = opened.tab_id;
  assert.notEqual(tab_id, workspace.tab_id);
  const enable = calls.findIndex(call => call[0] === 'Network.enable');
  const navigate = calls.findIndex(call => call[0] === 'update' && call[2]?.url?.includes('goods.html'));
  assert.ok(enable < navigate);
  const begin = async (id, path) => productNetworkEvent(api, {tabId: tab_id}, 'Network.requestWillBeSent',
    {requestId: id, request: {url: 'https://mobile.yangkeduo.com' + path, postData: '{"goods_id":"123"}'}});
  const finish = async (id, data) => {
    bodies.set(id, data);
    await productNetworkEvent(api, {tabId: tab_id}, 'Network.responseReceived', {requestId: id, type: 'Fetch', response: {status: 200}});
    await productNetworkEvent(api, {tabId: tab_id}, 'Network.loadingFinished', {requestId: id});
  };
  await begin('info', '/api/goods?goods_id=123');
  await finish('info', {goods_id: 123, goods_name: '商品'});
  await begin('old', '/proxy/api/reviews/123/list');
  const {phase} = await productOperation(api, 'product_reviews_begin', {tab_id});
  await finish('old', {data: [{review_id: 'old'}]});
  assert.equal((await productOperation(api, 'product_network', {tab_id, kind: 'reviews', phase})).received, false);
  await begin('current', '/proxy/api/reviews/123/list');
  await finish('current', {data: [{review_id: 'new'}, {review_id: 'new'}]});
  const result = await productOperation(api, 'product_network', {tab_id, kind: 'reviews', phase});
  assert.deepEqual(result.comments, [{review_id: 'new'}]);
  assert.equal(result.received, true);
  const next = await productOperation(api, 'product_reviews_begin', {tab_id});
  await begin('blocked', '/proxy/api/reviews/123/list');
  await finish('blocked', {error_code: 54001, data: []});
  assert.equal((await productOperation(api, 'product_network', {tab_id, kind: 'reviews', phase: next.phase})).received, false);
  assert.equal((await productOperation(api, 'product_network', {tab_id, kind: 'info'})).responses[0].data.goods_id, 123);
  await productOperation(api, 'product_close', {tab_id});
  assert.ok(tabs.has(workspace.tab_id));
  assert.equal(tabs.has(tab_id), false);
  await assert.rejects(productOperation(api, 'product_network', {tab_id}), /product_session_missing/);
});

test('gallery drags right to left and verifies every counter page instead of trusting three preloads', async () => {
  const {api, calls} = apiFixture();
  api.tabs.getZoom = async () => .5;
  const {tab_id} = await productOperation(api, 'product_open', {url: 'https://mobile.yangkeduo.com/goods.html'});
  let current = 1;
  api.scripting.executeScript = async options => {
    assert.equal(options.args[0].operation, 'gallery');
    return [{result: {current, total: 5, loaded: true, urls: ['preload1','preload2','preload3'],
      current_urls: [`https://images.example/${current}.jpg`], drag: {startX: 300, endX: 60, y: 120}}}];
  };
  const send = api.debugger.sendCommand;
  api.debugger.sendCommand = async (target, command, params) => {
    if (command === 'Input.dispatchMouseEvent' && params.type === 'mouseReleased') current = current % 5 + 1;
    return send(target, command, params);
  };
  const result = await productOperation(api, 'product_dom', {tab_id, operation: 'images'});
  assert.deepEqual(result.visited_pages, [1,2,3,4,5]);
  assert.equal(result.urls.length, 5);
  assert.equal(result.verification, 'counter-drag-v1');
  const events = calls.filter(call => call[0] === 'Input.dispatchMouseEvent').map(call => call[2]);
  assert.equal(events.filter(event => event.type === 'mouseReleased').length, 4);
  assert.equal(events[1].type, 'mousePressed');
  assert.equal(events[1].x, 150);
  const released = events.filter(event => event.type === 'mouseReleased');
  assert.ok(released.every(event => event.x === 30 && event.buttons === 0));
  assert.ok(events.filter(event => event.type === 'mouseMoved' && event.buttons === 1).length >= 48);
  assert.equal(calls.filter(call => call[0] === 'Input.dispatchTouchEvent').length, 0);
  assert.equal(calls.filter(call => call[0].startsWith('Emulation.') || ['window','zoom','zoomSettings'].includes(call[0])).length, 0);
});

test('gallery snapshot reads the explicit counter XPath and selects the current loaded image', async () => {
  const keys = ['document','location','XPathResult','innerWidth','innerHeight'];
  const old = Object.fromEntries(keys.map(key => [key, globalThis[key]]));
  const image = {complete: true, naturalWidth: 400, currentSrc: 'https://images.example/current.jpg',
    getAttribute: () => null, getBoundingClientRect: () => ({left: 300, right: 1068, top: 0, bottom: 768})};
  const root = {querySelectorAll: () => [image], getBoundingClientRect: () => ({left: 0, right: 1368, top: 0, bottom: 768})};
  globalThis.location = {hostname:'mobile.yangkeduo.com',protocol:'https:',href:'https://mobile.yangkeduo.com/goods.html'};
  globalThis.innerWidth=1368; globalThis.innerHeight=1400;
  globalThis.XPathResult={FIRST_ORDERED_NODE_TYPE:9};
  globalThis.document={evaluate: path => ({singleNodeValue: path.endsWith('/div[2]/span') ? {textContent:'2/8'} : {parentElement:root}})};
  try {
    const result=await productDom({operation:'gallery'});
    assert.equal(result.current,2); assert.equal(result.total,8);
    assert.deepEqual(result.current_urls,['https://images.example/current.jpg']);
    assert.ok(result.drag.startX >= 300 + 768 * .72 && result.drag.startX <= 300 + 768 * .86);
    assert.ok(result.drag.endX >= 300 + 768 * .14 && result.drag.endX <= 300 + 768 * .28);
    assert.ok(result.drag.y >= 768 * .55 && result.drag.y <= 768 * .8);
    assert.ok(Math.abs(result.drag.endY - result.drag.y) <= 768 * .025);
    assert.ok(result.drag.startX < 1068);
    const random = Math.random;
    try {
      Math.random = () => 0;
      const low = await productDom({operation:'gallery'});
      Math.random = () => .999;
      const high = await productDom({operation:'gallery'});
      assert.notDeepEqual(low.drag, high.drag);
      for (const page of [low, high]) {
        assert.ok(page.drag.startX > page.drag.endX);
        assert.ok(page.drag.endX > 300 && page.drag.startX < 1068);
        assert.ok(page.drag.endY > 0 && page.drag.endY < 768);
      }
    } finally {Math.random = random;}
  } finally {Object.assign(globalThis,old);}
});

test('gallery releases the mouse if a drag move fails and does not report completion', async () => {
  const {api, calls} = apiFixture();
  const {tab_id} = await productOperation(api, 'product_open', {url: 'https://mobile.yangkeduo.com/goods.html'});
  api.scripting.executeScript = async () => [{result: {current: 1, total: 2, loaded: true,
    current_urls: ['https://images.example/1.jpg'], drag: {startX: 300, endX: 60, y: 120}}}];
  const send = api.debugger.sendCommand;
  api.debugger.sendCommand = async (target, command, params) => {
    if (command === 'Input.dispatchMouseEvent' && params.type === 'mouseMoved' && params.buttons === 1) {
      throw new Error('drag_move_failed');
    }
    return send(target, command, params);
  };
  await assert.rejects(productOperation(api, 'product_dom', {tab_id, operation: 'images'}), /drag_move_failed/);
  const events = calls.filter(call => call[0] === 'Input.dispatchMouseEvent').map(call => call[2]);
  assert.equal(events.at(-1).type, 'mouseReleased');
  assert.equal(events.at(-1).buttons, 0);
});
