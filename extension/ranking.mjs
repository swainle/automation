// Ranking DOM operations are executed by the extension. No browser debugging port is opened.
export function allowedRankingPage(value) {
  try {
    const url = new URL(value);
    return !url.username && !url.password &&
      ((url.protocol === 'https:' && ['mobile.yangkeduo.com', 'yangkeduo.com',
        'mobile.pinduoduo.com', 'm.pinduoduo.com', 'www.pinduoduo.com', 'p.pinduoduo.com', 'p.yangkeduo.com'].includes(url.hostname)) ||
       (url.protocol === 'http:' && url.hostname === '127.0.0.1' && /^\/extension-test\/(ranking|product)\//.test(url.pathname)));
  } catch { return false; }
}

// Self-contained: serialized by chrome.scripting.executeScript, never supplied by Python.
export async function rankingDom({load = false, goods_id = null} = {}) {
  const url = new URL(location.href);
  if (url.username || url.password || !((url.protocol === 'https:' && ['mobile.yangkeduo.com', 'yangkeduo.com',
    'mobile.pinduoduo.com', 'm.pinduoduo.com', 'www.pinduoduo.com'].includes(url.hostname)) ||
    (url.protocol === 'http:' && url.hostname === '127.0.0.1' && url.pathname.startsWith('/extension-test/ranking/')))) {
    throw new Error('page_not_allowed');
  }
  const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
  if (load) {
    const deadline = Date.now() + 12000;
    while (!document.querySelector('[data-ranking-goods-id]') && Date.now() < deadline) await pause(200);
    if (!document.querySelector('[data-ranking-goods-id]')) throw new Error('ranking_cards_not_found');
    for (let round = 0; round < 8; round++) {
      window.scrollTo(0, document.documentElement.scrollHeight);
      await pause(800);
    }
    if (!document.getElementById('__pddRankingFreeze')) {
      const style = document.createElement('style');
      style.id = '__pddRankingFreeze';
      style.textContent = '*,*::before,*::after {animation:none!important;transition:none!important;scroll-behavior:auto!important}';
      document.documentElement.appendChild(style);
    }
  }
  const nodes = [...document.querySelectorAll('[data-ranking-goods-id]')];
  if (goods_id !== null) {
    const node = nodes.find(item => item.getAttribute('data-ranking-goods-id') === goods_id);
    if (!node) throw new Error('ranking_card_not_found');
    const reference = document.querySelector('#main > div:first-of-type');
    const bottom = reference?.getBoundingClientRect().bottom || 0;
    window.scrollBy(0, node.getBoundingClientRect().top - bottom);
    await pause(800);
    const rect = node.getBoundingClientRect();
    if (rect.width <= 0 || rect.height <= 0 || rect.bottom <= 0 || rect.top >= innerHeight) {
      throw new Error('ranking_card_not_visible');
    }
  }
  const seen = new Set();
  const cards = [];
  for (const node of nodes) {
    const id = node.getAttribute('data-ranking-goods-id');
    if (!id || seen.has(id)) continue;
    seen.add(id);
    const text = node.innerText || '';
    const titleNode = node.querySelector('[title]');
    const title = (titleNode?.getAttribute('title') || titleNode?.innerText ||
      node.querySelector('.QGXh9I1y')?.innerText || '').trim();
    const badge = node.querySelector('.xxxVM4ts');
    const price = text.match(/[¥￥]\s*([\d.]+)/)?.[1] || '';
    let listing = '';
    for (const image of node.querySelectorAll('img')) {
      const match = (image.getAttribute('src') || '').match(/(\d{4}-\d{2}-\d{2})/);
      if (match) { listing = match[1]; break; }
    }
    cards.push({goods_id: id, title, text, rank: badge?.innerText?.trim() || String(cards.length + 1), price, listing});
  }
  const tabs = [...document.querySelectorAll('.Bt00keaq')].map(node => node.innerText.trim()).filter(Boolean);
  const heading = document.evaluate('//*[@id="main"]/div[1]/div/div[2]/div[3]/div[2]/div/div[1]',
    document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null).singleNodeValue?.textContent?.trim() || '';
  const description = [
    '//*[@id="main"]/div[1]/div/div[2]/div[3]/div[2]/div/div[2]',
    '//*[@id="main"]/div[1]/div/div[1]/div[1]/div[1]'
  ].map(xpath => document.evaluate(xpath, document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null)
    .singleNodeValue?.textContent?.trim() || '').find(Boolean) || '';
  return {tabs, cards, heading, description, viewport: {width: innerWidth, height: innerHeight}};
}

async function registry(api) {
  return (await api.storage.session.get('rankingTabs')).rankingTabs || {};
}

async function owned(api, tabId, {closing = false} = {}) {
  if (!Number.isInteger(tabId)) throw new Error('invalid_tab_id');
  const settings = (await registry(api))[tabId];
  if (!settings) throw new Error('ranking_tab_not_owned');
  const tab = await api.tabs.get(tabId);
  const newBlank = !tab.url && !settings.navigated;
  if (!closing && !newBlank && tab.url !== 'about:blank' && !allowedRankingPage(tab.url)) throw new Error('page_not_allowed');
  return {tab, settings};
}

async function attachDebugger(api, tabId, settings) {
  const target = {tabId};
  try { await api.debugger.sendCommand(target, 'Page.getLayoutMetrics'); }
  catch { await api.debugger.attach(target, '1.3'); }
}

export async function rankingTarget(api, tabId) {
  const {tab, settings} = await owned(api, tabId);
  await attachDebugger(api, tabId, settings);
  return tab;
}

export async function captureViewport(api, tabId) {
  await rankingTarget(api, tabId);
  const target = {tabId};
  const {cssLayoutViewport: view} = await api.debugger.sendCommand(target, 'Page.getLayoutMetrics');
  if (!view || ![view.pageX, view.pageY, view.clientWidth, view.clientHeight].every(Number.isFinite) ||
      view.pageX < 0 || view.pageY < 0 || view.clientWidth <= 0 || view.clientHeight <= 0 ||
      view.clientWidth * view.clientHeight > 25000000) throw new Error('ranking_viewport_invalid');
  const image = await api.debugger.sendCommand(target, 'Page.captureScreenshot', {
    format: 'jpeg', quality: 95, captureBeyondViewport: true,
    clip: {x: view.pageX, y: view.pageY, width: view.clientWidth, height: view.clientHeight, scale: 1}
  });
  if (!image.data || image.data.length > 22000000) throw new Error('screenshot_too_large');
  return {format: 'jpeg', data: image.data, width: view.clientWidth, height: view.clientHeight};
}

async function probe(api, tabId, params) {
  const result = await api.scripting.executeScript({target: {tabId, frameIds: [0]}, func: rankingDom, args: [params]});
  if (!result[0]?.result) throw new Error('ranking_dom_failed');
  return result[0].result;
}

export async function rankingOperation(api, action, params) {
  if (action === 'ranking_prepare' || (action === 'ranking_open' && params.reuse_workspace)) {
    const workspace = (await api.storage.session.get('rankingWorkspaceTab')).rankingWorkspaceTab;
    if (Number.isInteger(workspace)) {
      try {
        const existing = await owned(api, workspace);
        const settings = {navigated: Boolean(existing.settings.navigated)};
        await api.storage.session.set({rankingTabs: {...await registry(api), [workspace]: settings}});
        await attachDebugger(api, workspace, settings);
        await api.tabs.update(workspace, {active: true});
        return {tab_id: workspace, ...settings, persistent: true, reused: true, protocol: 2};
      } catch {
        // A manually redirected page is no longer a task page; release it without closing it.
        await api.debugger.detach({tabId: workspace}).catch(() => {});
        const tabs = await registry(api);
        delete tabs[workspace];
        await api.storage.session.set({rankingTabs: tabs});
      }
    }
    if (action === 'ranking_prepare') {
      const created = await rankingOperation(api, 'ranking_open', {...params, reuse_workspace: false});
      await api.storage.session.set({rankingWorkspaceTab: created.tab_id});
      return {...created, persistent: true, reused: false};
    }
  }
  if (action === 'ranking_open') {
    const tab = params.new_window
      ? (await api.windows.create({url: 'about:blank', focused: true, type: 'normal'})).tabs[0]
      : await api.tabs.create({url: 'about:blank', active: true});
    const settings = {};
    await api.storage.session.set({rankingTabs: {...await registry(api), [tab.id]: settings}});
    try {
      // Attach only; manual window, zoom and viewport settings remain untouched.
      await attachDebugger(api, tab.id, settings);
      return {tab_id: tab.id, ...settings, protocol: 2};
    } catch (error) {
      await rankingOperation(api, 'ranking_close', {tab_id: tab.id});
      throw error;
    }
  }
  const {tab, settings} = await owned(api, params.tab_id, {closing: action === 'ranking_close'});
  if (action === 'ranking_close') {
    await api.debugger.detach({tabId: tab.id}).catch(() => {});
    await api.tabs.remove(tab.id);
    const tabs = await registry(api);
    delete tabs[tab.id];
    await api.storage.session.set({rankingTabs: tabs});
    if ((await api.storage.session.get('rankingWorkspaceTab')).rankingWorkspaceTab === tab.id) {
      await api.storage.session.set({rankingWorkspaceTab: null});
    }
    return {closed: true};
  }
  await attachDebugger(api, tab.id, settings);
  if (action === 'ranking_navigate') {
    if (!allowedRankingPage(params.url)) throw new Error('page_not_allowed');
    const previous = tab.url;
    let started = false;
    const onUpdate = (id, change) => { if (id === tab.id && (change.status === 'loading' || change.url)) started = true; };
    api.tabs.onUpdated?.addListener(onUpdate);
    try {
      const updated = await api.tabs.update(tab.id, {url: params.url, active: true});
      await api.storage.session.set({rankingTabs: {...await registry(api), [tab.id]: {...settings, navigated: true}}});
      started ||= updated?.status === 'loading' || Boolean(updated?.pendingUrl);
      const deadline = Date.now() + 25000;
      while (Date.now() < deadline) {
        const current = await api.tabs.get(tab.id);
        if (current.status === 'loading' || current.pendingUrl) started = true;
        if (current.url && current.url !== 'about:blank' && !allowedRankingPage(current.url)) throw new Error('page_not_allowed');
        if (current.status === 'complete' && !current.pendingUrl && allowedRankingPage(current.url) &&
            (started || current.url !== previous || current.url === params.url)) return {tab_id: tab.id};
        await new Promise(resolve => setTimeout(resolve, 100));
      }
      throw new Error('ranking_page_timeout');
    } finally { api.tabs.onUpdated?.removeListener(onUpdate); }
  }
  if (action === 'ranking_read') return await probe(api, tab.id, {load: params.load === true});
  if (action === 'ranking_screenshot') {
    if (typeof params.goods_id !== 'string' || !params.goods_id || params.goods_id.length > 200) throw new Error('invalid_goods_id');
    await api.tabs.update(tab.id, {active: true});
    await probe(api, tab.id, {goods_id: params.goods_id});
    if (!allowedRankingPage((await api.tabs.get(tab.id)).url)) throw new Error('page_not_allowed');
    const target = {tabId: tab.id};
    const metrics = await api.debugger.sendCommand(target, 'Page.getLayoutMetrics');
    const view = metrics.cssLayoutViewport;
    if (!view || ![view.pageX, view.pageY, view.clientWidth, view.clientHeight].every(Number.isFinite) ||
        view.pageX < 0 || view.pageY < 0 || view.clientWidth <= 0 || view.clientHeight <= 0 ||
        view.clientWidth * view.clientHeight > 25000000) throw new Error('ranking_viewport_invalid');
    const image = await api.debugger.sendCommand(target, 'Page.captureScreenshot', {
      format: 'jpeg', quality: 95, captureBeyondViewport: true,
      clip: {x: view.pageX, y: view.pageY, width: view.clientWidth, height: view.clientHeight, scale: 1}
    });
    if (!image.data || image.data.length > 22000000) throw new Error('screenshot_too_large');
    return {format: 'jpeg', data: image.data, width: view.clientWidth, height: view.clientHeight};
  }
  throw new Error('unsupported_action');
}
