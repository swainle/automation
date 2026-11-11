import assert from 'node:assert/strict';
import {rankingDom} from '../../../extension/ranking.mjs';

export function apiFixture() {
  const state = {}, tabs = new Map(), attached = new Set(), calls = [];
  let next = 10;
  const api = {
    storage: {session: {get: async key => ({[key]: state[key]}), set: async value => Object.assign(state, value)}},
    tabs: {
      create: async params => { const tab = {id: next++, windowId: 1, ...params, status: 'complete'}; tabs.set(tab.id, tab); return tab; },
      get: async id => { if (!tabs.has(id)) throw new Error('missing_tab'); return tabs.get(id); },
      update: async (id, params) => { calls.push(['update', id, params]); Object.assign(tabs.get(id), params); return tabs.get(id); },
      remove: async id => { calls.push(['remove', id]); tabs.delete(id); },
      setZoomSettings: async (...args) => calls.push(['zoomSettings', ...args]),
      setZoom: async (...args) => calls.push(['zoom', ...args]),
      onUpdated: {addListener() {}, removeListener() {}}
    },
    windows: {update: async (...args) => { calls.push(['window', ...args]); return {id: args[0], ...args[1]}; }},
    debugger: {
      attach: async ({tabId}) => { calls.push(['attach', tabId]); attached.add(tabId); },
      detach: async ({tabId}) => { calls.push(['detach', tabId]); attached.delete(tabId); },
      sendCommand: async ({tabId}, command, params) => {
        calls.push([command, tabId, params]);
        if (!attached.has(tabId)) throw new Error('not_attached');
        if (command === 'Page.getLayoutMetrics') return {cssLayoutViewport: {pageX: 0, pageY: 120, clientWidth: 390, clientHeight: 844}};
        if (command === 'Page.captureScreenshot') return {data: 'jpeg-base64'};
        return {};
      }
    },
    scripting: {executeScript: async options => {
      calls.push(['dom', options]);
      assert.equal(options.func, rankingDom);
      return [{result: {tabs: ['畅销榜'], cards: [{goods_id: '123'}]}}];
    }}
  };
  return {api, state, tabs, attached, calls};
}

