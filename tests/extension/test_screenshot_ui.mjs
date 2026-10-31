import test from 'node:test';
import assert from 'node:assert/strict';
import {screenshotUi} from '../../extension/screenshot-ui.mjs';

test('both known banner layers are hidden even when expanded container has zero height', async () => {
  const make = (height, original, originalPriority = '') => {
    let display = original, priority = originalPriority;
    return {innerText: '拼多多 · 安全守护程序 安装', isConnected: true,
      getBoundingClientRect: () => ({top: 56, height, width: 1905}), querySelector: () => null,
      style: {getPropertyValue: () => display, getPropertyPriority: () => priority,
        setProperty: (key, value, level) => {display = value; priority = level;},
        removeProperty: () => {display = ''; priority = '';}}};
  };
  const tip = make(32, ''), content = make(0, 'block', 'important');
  globalThis.location = {origin: 'https://mms.pinduoduo.com'};
  globalThis.document = {querySelectorAll: () => [tip, content]};
  globalThis.getComputedStyle = node => ({display: node.style.getPropertyValue('display'), visibility: 'visible'});
  globalThis.requestAnimationFrame = callback => callback();
  try {
    assert.equal((await screenshotUi()).count, 2);
    assert.equal(tip.style.getPropertyValue('display'), 'none');
    assert.equal(content.style.getPropertyValue('display'), 'none');
    await screenshotUi(); // 重复准备不能覆盖原始样式。
    await screenshotUi(true);
    assert.equal(tip.style.getPropertyValue('display'), '');
    assert.equal(content.style.getPropertyValue('display'), 'block');
    assert.equal(content.style.getPropertyPriority('display'), 'important');
  } finally {
    for (const key of ['location', 'document', 'getComputedStyle', 'requestAnimationFrame', '__automationScreenshotBanner']) delete globalThis[key];
  }
});

test('only matching installation banner is hidden and original display restored', async () => {
  const make = text => {
    let display = 'flex', priority = '';
    return {innerText: text, isConnected: true,
      getBoundingClientRect: () => ({top: 56, height: 128, width: 1000}),
      querySelector: () => null, querySelectorAll: () => [],
      style: {getPropertyValue: () => display, getPropertyPriority: () => priority,
        setProperty: (key, value, level) => {display = value; priority = level;},
        removeProperty: () => {display = '';}}};
  };
  const banner = make('拼多多·安全守护程 商家必备 安装');
  const business = make('订单安全提醒');
  globalThis.location = {origin: 'https://mms.pinduoduo.com'};
  globalThis.innerWidth = 1000;
  globalThis.document = {querySelectorAll: selector => selector.startsWith('.mms-main-safe') ? [] : [business, banner]};
  globalThis.getComputedStyle = node => ({display: node.style.getPropertyValue('display'), visibility: 'visible'});
  globalThis.requestAnimationFrame = callback => callback();
  try {
    assert.equal((await screenshotUi()).hidden, true);
    assert.equal(banner.style.getPropertyValue('display'), 'none');
    assert.equal(business.style.getPropertyValue('display'), 'flex');
    await screenshotUi(true);
    assert.equal(banner.style.getPropertyValue('display'), 'flex');
    assert.equal(banner.style.getPropertyPriority('display'), '');
  } finally {
    for (const key of ['location', 'innerWidth', 'document', 'getComputedStyle', 'requestAnimationFrame', '__automationScreenshotBanner']) delete globalThis[key];
  }
});
