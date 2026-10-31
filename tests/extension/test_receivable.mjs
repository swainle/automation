import test from 'node:test';
import assert from 'node:assert/strict';
import {probeReceivable, probeNotification, closeNotification} from '../../extension/receivable.mjs';

test('shipping location prompt closes without selecting a location and drains successive dialogs', async () => {
  const titles = ['请选择您的主要发货地？请选择省份 确认', '今日平台已支持您以下申诉，打款成功1元',
    '现有1笔订单，您可一键发起维权申诉！', '现有 12 笔订单，您可一键发起维权申诉！'];
  let index = 0, clicks = 0;
  const modal = {parentElement: null, get innerText() { return titles[index] || ''; },
    getBoundingClientRect: () => ({width: index < titles.length ? 648 : 0, height: 400}),
    querySelectorAll: () => [close]};
  const close = {parentElement: modal, getBoundingClientRect: () => ({width: 32, height: 32}),
    click() { clicks++; index++; }};
  globalThis.location = {origin: 'https://mms.pinduoduo.com'};
  globalThis.getComputedStyle = () => ({display: 'block', visibility: 'visible', opacity: '1'});
  globalThis.XPathResult = {FIRST_ORDERED_NODE_TYPE: 9};
  globalThis.document = {querySelectorAll: () => [modal], evaluate: () => ({singleNodeValue: null})};
  try {
    assert.deepEqual(probeNotification(false), {visible: true, clicked: false});
    assert.equal(clicks, 0);
    modal.querySelectorAll = () => [close, close];
    assert.throws(() => probeNotification(true), /shipping_notification_close_not_unique/);
    assert.equal(clicks, 0);
    modal.querySelectorAll = () => [close];
    const api = {scripting: {executeScript: async ({func, args}) => [{result: func(...args)}]}};
    assert.deepEqual(await closeNotification(api, 1), {closed: true, clicked: true});
    assert.equal(clicks, 4);
    index = 0;
    titles[0] = '业务说明：请选择您的主要发货地';
    assert.deepEqual(probeNotification(true), {visible: false});
    assert.equal(clicks, 4);
    titles[0] = '其他业务说明：现有1笔订单，您可一键发起维权申诉！';
    assert.deepEqual(probeNotification(true), {visible: false});
    assert.equal(clicks, 4);
  } finally {
    for (const key of ['location', 'getComputedStyle', 'XPathResult', 'document']) delete globalThis[key];
  }
});

test('appeal payment dialog closes by scoped class and text despite changed body order', async () => {
  let shown = true, clicks = 0;
  const button = {parentElement: null, getBoundingClientRect: () => ({width: 32, height: 32}),
    click() { shown = false; clicks++; }};
  const modal = {innerText: '今日平台已支持您以下申诉，打款成功12.33元', parentElement: null,
    getBoundingClientRect: () => ({width: shown ? 648 : 0, height: shown ? 400 : 0}),
    querySelectorAll: () => [button]};
  button.parentElement = modal;
  globalThis.location = {origin: 'https://mms.pinduoduo.com'};
  globalThis.getComputedStyle = () => ({display: 'block', visibility: 'visible', opacity: '1'});
  globalThis.XPathResult = {FIRST_ORDERED_NODE_TYPE: 9};
  globalThis.document = {querySelectorAll: () => [modal], evaluate: () => ({singleNodeValue: null})};
  try {
    assert.deepEqual(probeNotification(false), {visible: true, clicked: false});
    assert.equal(clicks, 0);
    const api = {scripting: {executeScript: async ({func, args}) => [{result: func(...args)}]}};
    assert.deepEqual(await closeNotification(api, 1), {closed: true, clicked: true});
    assert.equal(clicks, 1);
    shown = true;
    modal.innerText = '其他业务弹窗';
    assert.deepEqual(probeNotification(true), {visible: false});
    assert.equal(clicks, 1);
    modal.innerText = '今日平台已支持您以下申诉，打款成功12.33元';
    modal.querySelectorAll = () => [button, button];
    assert.throws(() => probeNotification(true), /appeal_notification_close_not_unique/);
    assert.equal(clicks, 1);
  } finally {
    for (const key of ['location', 'getComputedStyle', 'XPathResult', 'document']) delete globalThis[key];
  }
});

test('notification clicks a zero-size icon using its parent hit area and confirms disappearance', async () => {
  let clicks = 0;
  const root = {id: 'umd-msgbox-handler', parentElement: null};
  const parent = {parentElement: root, getBoundingClientRect: () => ({width: 24, height: 24})};
  let icon = {parentElement: parent, getBoundingClientRect: () => ({width: 0, height: 0}),
    click() { clicks++; icon = null; }};
  globalThis.location = {origin: 'https://mms.pinduoduo.com'};
  globalThis.getComputedStyle = () => ({display: 'block', visibility: 'visible', opacity: '1'});
  globalThis.XPathResult = {FIRST_ORDERED_NODE_TYPE: 9};
  globalThis.document = {querySelectorAll: () => [], evaluate: () => ({singleNodeValue: icon})};
  try {
    const api = {scripting: {executeScript: async ({func, args}) => [{result: func(...args)}]}};
    assert.deepEqual(probeNotification(false), {visible: true, clicked: false});
    assert.equal(clicks, 0);
    assert.deepEqual(await closeNotification(api, 1), {closed: true, clicked: true});
    assert.equal(clicks, 1);
    assert.deepEqual(probeNotification(false), {visible: false});
  } finally {
    for (const key of ['location', 'getComputedStyle', 'XPathResult', 'document']) delete globalThis[key];
  }
});

test('notification is closed before clicking nested reveal button; visible zero is preserved', () => {
  const events = [];
  const node = text => ({innerText: text, getBoundingClientRect: () => ({width: 10, height: 10})});
  const anchor = node('待收货'), amount = node('查看金额'), panel = node('');
  panel.parentElement = {children: [panel]};
  let notification = {...node(''), click() {events.push('close'); notification = null;}};
  const button = {...node(''), click() {events.push('reveal'); amount.innerText = '待到账0.00';}};
  globalThis.location = {origin: 'https://mms.pinduoduo.com'};
  globalThis.getComputedStyle = () => ({display: 'block', visibility: 'visible', opacity: '1'});
  globalThis.XPathResult = {FIRST_ORDERED_NODE_TYPE: 9};
  globalThis.document = {evaluate(xpath) {
    const value = xpath.includes('umd-msgbox-handler') ? notification :
      xpath.includes('ancestor::') ? panel : xpath === '../p[3]/span' ? amount :
      xpath.endsWith('/span/span/span') ? button : anchor;
    return {singleNodeValue: value};
  }};
  try {
    assert.deepEqual(probeReceivable(true), {ready: false});
    assert.equal(probeReceivable(true).clicked, true);
    assert.equal(probeReceivable(true).amount, '0.00');
    assert.deepEqual(events, ['close', 'reveal']);
  } finally {
    for (const key of ['location', 'getComputedStyle', 'XPathResult', 'document']) delete globalThis[key];
  }
});
