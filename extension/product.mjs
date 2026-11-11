import {allowedRankingPage, rankingOperation, rankingTarget, captureViewport} from './ranking.mjs';

const sessions = new Map();
const reviewPath = value => /^\/proxy\/api\/reviews\/[^/]+\/list$/.test(new URL(value).pathname);

// Capture identity belongs to a single product tab and request phase, not the browser.
export async function productNetworkEvent(api, source, method, params) {
  const session = sessions.get(source.tabId);
  if (!session) return;
  if (method === 'Network.requestWillBeSent') {
    let url;
    try { url = new URL(params.request.url); } catch { return; }
    const permitted = ['pinduoduo.com', 'yangkeduo.com'].some(domain => url.hostname === domain || url.hostname.endsWith('.' + domain)) ||
      (url.protocol === 'http:' && url.hostname === '127.0.0.1');
    if (!permitted) return;
    let body = null;
    try { body = JSON.parse(params.request.postData || 'null'); } catch { /* not JSON */ }
    session.requests.set(params.requestId, {url: url.href, body, phase: session.phase});
  } else if (method === 'Network.responseReceived') {
    const item = session.requests.get(params.requestId);
    if (item) Object.assign(item, {status: params.response.status, type: params.type});
  } else if (method === 'Network.loadingFailed') {
    session.requests.delete(params.requestId);
  } else if (method === 'Network.loadingFinished') {
    const item = session.requests.get(params.requestId);
    session.requests.delete(params.requestId);
    if (!item || !['XHR', 'Fetch'].includes(item.type) || item.status >= 400) return;
    const pending = (async () => {
      try {
        const response = await api.debugger.sendCommand({tabId: source.tabId}, 'Network.getResponseBody', {requestId: params.requestId});
        if (response.body.length > 8 * 1024 * 1024) throw new Error('product_response_too_large');
        const text = response.base64Encoded ? new TextDecoder().decode(Uint8Array.from(atob(response.body), c => c.charCodeAt(0))) : response.body;
        let data;
        try { data = JSON.parse(text); } catch { return; }
        session.bytes += new TextEncoder().encode(text).length;
        if (session.bytes > 16 * 1024 * 1024) throw new Error('product_capture_too_large');
        session.responses.push({...item, data});
      } catch (error) {
        // An unrelated evicted/empty response is not a failed product; required
        // product and review data are independently checked by the caller.
        if (['product_capture_too_large', 'product_response_too_large'].includes(error.message)) session.error = error.message;
      }
    })();
    session.pending.add(pending);
    try { await pending; } finally { session.pending.delete(pending); }
  }
}

// Only fixed operations are accepted. The caller cannot provide JavaScript or selectors.
export async function productDom({operation, selections = [], name = ''}) {
  const host = location.hostname;
  if (!(['mobile.yangkeduo.com', 'yangkeduo.com', 'mobile.pinduoduo.com', 'm.pinduoduo.com',
    'www.pinduoduo.com', 'p.pinduoduo.com', 'p.yangkeduo.com'].includes(host) && location.protocol === 'https:') &&
    !(host === '127.0.0.1' && location.protocol === 'http:' && location.pathname.startsWith('/extension-test/product/'))) throw new Error('page_not_allowed');
  const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
  const visible = node => {
    if (!node) return false;
    const rect = node.getBoundingClientRect(), style = getComputedStyle(node);
    return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
  };
  const xpath = value => document.evaluate(value, document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null).singleNodeValue;
  const panel = () => [...document.querySelectorAll('#main .sku-plus1')].find(visible);
  const wait = async predicate => {
    const end = Date.now() + 7000;
    while (Date.now() < end) { const value = predicate(); if (value) return value; await pause(100); }
    throw new Error('product_elements_timeout');
  };
  const openSku = async () => {
    if (!panel()) {
      const button = [...document.querySelectorAll('button,[role="button"]')].find(node => visible(node) && /免拼购买|立即购买|单独购买|发起拼单|立即拼单/.test(node.innerText));
      if (!button) throw new Error('product_buy_button_not_found');
      button.click();
    }
    return await wait(() => { const node = panel(); return node && [...node.querySelectorAll('.F7sZG3xe')].some(visible) && node; });
  };
  if (operation === 'info') {
    if (!document.getElementById('__pddProductFreeze')) {
      const style = document.createElement('style');
      style.id = '__pddProductFreeze';
      style.textContent = '*,*::before,*::after {animation:none!important;transition:none!important}';
      document.documentElement.appendChild(style);
    }
    const sold = xpath('//*[@id="main"]/div/div[1]/span');
    return {url: location.href, sold_out: visible(sold) && (sold.innerText || '').includes('商品已售罄，推荐以下相似商品')};
  }
  if (operation === 'gallery') {
    const counter = xpath('//*[@id="main"]/div/div[1]/div[2]/span');
    const match = (counter?.textContent || counter?.innerText || '').trim().match(/^(\d+)\s*[/／]\s*(\d+)$/);
    if (!match) throw new Error('product_gallery_counter_missing');
    const current = Number(match[1]), total = Number(match[2]);
    if (current < 1 || total < current || total > 500) throw new Error('product_gallery_counter_invalid');
    const root = xpath('//*[@id="main"]/div/div[1]/div[1]/div/div[1]')?.parentElement;
    if (!root) throw new Error('product_gallery_missing');
    const rect = root.getBoundingClientRect();
    const left = Math.max(0, rect.left), right = Math.min(innerWidth, rect.right);
    const top = Math.max(0, rect.top), bottom = Math.min(innerHeight, rect.bottom);
    if (right - left < 50 || bottom - top < 20) throw new Error('product_gallery_not_in_view');
    const urls = [], visibleUrls = [];
    for (const image of root.querySelectorAll('img')) {
      const raw = image.getAttribute('data-original') || image.getAttribute('data-src') ||
        image.getAttribute('data-lazy-src') || image.currentSrc || image.getAttribute('src');
      if (!raw) continue;
      const url = new URL(raw, location.href);
      if (!['https:', 'http:'].includes(url.protocol)) continue;
      urls.push(url.href);
      const bounds = image.getBoundingClientRect();
      if (image.complete && image.naturalWidth > 0 && bounds.right > left && bounds.left < right &&
          bounds.bottom > top && bounds.top < bottom) visibleUrls.push({url: url.href, bounds: {
            left: Math.max(bounds.left, left), right: Math.min(bounds.right, right),
            top: Math.max(bounds.top, top), bottom: Math.min(bounds.bottom, bottom)},
            area: (Math.min(bounds.right, right) - Math.max(bounds.left, left)) *
              (Math.min(bounds.bottom, bottom) - Math.max(bounds.top, top))});
    }
    visibleUrls.sort((a, b) => b.area - a.area);
    const image = visibleUrls[0]?.bounds;
    const random = (min, max) => min + Math.random() * (max - min);
    const width = image ? image.right - image.left : 0, height = image ? image.bottom - image.top : 0;
    const y = image ? image.top + height * random(.55, .8) : 0;
    return {current, total, urls: [...new Set(urls)], current_urls: visibleUrls.length ? [visibleUrls[0].url] : [], loaded: visibleUrls.length > 0,
      image_bounds: image || null,
      drag: image ? {startX: image.left + width * random(.72, .86),
        endX: image.left + width * random(.14, .28), y,
        endY: y + height * random(-.025, .025)} : null};
  }
  if (operation === 'details') {
    for (let round = 0; round < 8; round++) { window.scrollBy(0, 4000); await pause(600); }
    const box = document.querySelector('#main .pApAfrwF')?.nextElementSibling;
    return {urls: box ? [...box.querySelectorAll('img')].map(img => img.getAttribute('src')).filter(Boolean).map(url => new URL(url, location.href).href) : []};
  }
  if (operation === 'sku_dimensions') {
    const node = await openSku();
    return {dimensions: [...node.querySelectorAll('.bIhLWVqm')].map((dim, index) => ({index,
      name: dim.querySelector('.sku-specs-key')?.innerText || '',
      options: [...dim.querySelectorAll('.F7sZG3xe')].map((option, index) => ({index, text: option.innerText || ''}))}))};
  }
  if (operation === 'sku_select') {
    await openSku();
    for (const choice of selections) {
      if (!Number.isInteger(choice.dimension) || !Number.isInteger(choice.option) || choice.dimension < 0 || choice.option < 0) throw new Error('invalid_sku_selection');
      const dim = panel()?.querySelectorAll('.bIhLWVqm')[choice.dimension];
      const option = dim?.querySelectorAll('.F7sZG3xe')[choice.option];
      if (!visible(option)) throw new Error('product_sku_option_missing');
      option.scrollIntoView({block: 'center'});
      if (!option.classList.contains('hr353bdX')) option.click();
      await pause(300);
      const selected = panel()?.querySelectorAll('.bIhLWVqm')[choice.dimension]?.querySelectorAll('.F7sZG3xe')[choice.option];
      if (!selected?.classList.contains('hr353bdX')) throw new Error('product_sku_not_selected');
    }
    await pause(1000);
    const current = panel();
    if (!current) throw new Error('product_sku_panel_closed');
    const image = current.querySelector('img')?.getAttribute('src');
    return {image: image ? new URL(image, location.href).href : '', price: current.querySelector('.ujEqGzEB')?.innerText || ''};
  }
  if (operation === 'sku_close') {
    const node = panel();
    if (node) {
      const close = node.querySelector('.GvCXfXbo');
      if (close) close.click();
      else document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true}));
      await wait(() => !panel());
    }
    return {closed: true};
  }
  if (operation === 'reviews_enter') {
    if (visible(document.querySelector('#goods-comments-list'))) return {opened: true};
    for (const selector of ["//div[starts-with(normalize-space(text()),'商品评价')]/following-sibling::span",
      '//*[@id="main"]/div/div[2]/div[17]/div[1]/div/span', "//span[normalize-space(text())='查看全部']"]) {
      const node = xpath(selector);
      if (visible(node)) { node.click(); return {triggered: true}; }
    }
    throw new Error('product_reviews_entry_missing');
  }
  if (operation === 'reviews_ready') return {ready: visible(document.querySelector('#goods-comments-list'))};
  if (operation === 'reviews_tags') {
    if (!visible(document.querySelector('#goods-comments-list'))) throw new Error('product_reviews_not_open');
    const tags = [...document.querySelectorAll('#main ul li')].filter(node => {
      const [r, g, b] = (getComputedStyle(node).backgroundColor.match(/\d+/g) || []).map(Number);
      return node.classList.contains('pEu3DjzW') || (r >= 220 && r <= 249 && Math.abs(r - g) <= 3 && Math.abs(g - b) <= 3);
    }).map(node => {
      const name = node.innerText.trim();
      const count = name.match(/[（(]\s*(\d+)\s*[)）]\s*$/);
      return {name, count: count ? Number(count[1]) : null};
    }).filter(tag => tag.name);
    return {names: tags.map(tag => tag.name), tags};
  }
  if (operation === 'reviews_tag') {
    window.scrollTo(0, 0);
    let tag = [...document.querySelectorAll('#main ul li')].find(node => node.innerText.trim() === name && visible(node));
    if (!tag) {
      document.querySelector('#main .mQMJpeQt')?.click();
      tag = await wait(() => [...document.querySelectorAll('#main ul li')].find(node => node.innerText.trim() === name && visible(node)));
    }
    tag.click();
    return {clicked: true};
  }
  if (operation === 'reviews_scroll') { window.scrollTo(0, document.documentElement.scrollHeight); return {scrolled: true}; }
  throw new Error('unsupported_product_operation');
}

async function gallerySnapshot(api, tabId) {
  const result = await api.scripting.executeScript({target: {tabId, frameIds: [0]}, func: productDom, args: [{operation: 'gallery'}]});
  if (!result[0]?.result) throw new Error('product_gallery_probe_failed');
  return result[0].result;
}

async function collectGallery(api, tabId) {
  const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
  const random = (min, max) => min + Math.random() * (max - min);
  const zoom = api.tabs.getZoom ? await api.tabs.getZoom(tabId) : 1;
  if (!Number.isFinite(zoom) || zoom <= 0) throw new Error('product_gallery_zoom_invalid');
  const ready = async previous => {
    for (let attempt = 0; attempt < 20; attempt++) {
      const snapshot = await gallerySnapshot(api, tabId);
      if (snapshot.loaded && (previous == null || snapshot.current !== previous)) return snapshot;
      await pause(200);
    }
    throw new Error(previous == null ? 'product_gallery_image_not_loaded' : 'product_gallery_drag_did_not_advance');
  };
  let page = await ready(null);
  const total = page.total, visited = new Set(), pages = new Map();
  while (visited.size < total) {
    if (page.total !== total || visited.has(page.current)) throw new Error('product_gallery_counter_inconsistent');
    visited.add(page.current);
    // Keep carousel ordering: record the visible/current image rather than preloads.
    pages.set(page.current, page.current_urls);
    if (visited.size === total) break;
    const startX = page.drag.startX * zoom, endX = page.drag.endX * zoom, y = page.drag.y * zoom;
    const endY = (page.drag.endY ?? page.drag.y) * zoom;
    const steps = Math.floor(random(12, 19));
    await pause(random(350, 1000));
    await api.debugger.sendCommand({tabId}, 'Input.dispatchMouseEvent', {
      type: 'mouseMoved', x: startX, y, button: 'none', buttons: 0});
    await pause(random(50, 120));
    await api.debugger.sendCommand({tabId}, 'Input.dispatchMouseEvent', {
      type: 'mousePressed', x: startX, y, button: 'left', buttons: 1, clickCount: 1});
    let releaseX = startX, releaseY = y;
    try {
      await pause(random(35, 100));
      for (let step = 1; step <= steps; step++) {
        const progress = 1 - (1 - step / steps) ** 2;
        releaseX = startX + (endX - startX) * progress;
        releaseY = y + (endY - y) * progress;
        await api.debugger.sendCommand({tabId}, 'Input.dispatchMouseEvent', {
          type: 'mouseMoved', x: releaseX, y: releaseY, button: 'left', buttons: 1});
        await pause(random(15, 32));
      }
    } finally {
      await api.debugger.sendCommand({tabId}, 'Input.dispatchMouseEvent', {
        type: 'mouseReleased', x: releaseX, y: releaseY, button: 'left', buttons: 0, clickCount: 1});
    }
    const previous = page.current;
    await pause(random(450, 1100));
    page = await ready(previous);
    if (page.current !== previous % total + 1) throw new Error('product_gallery_skipped_page');
  }
  const urls = [...new Set([...pages].sort((a, b) => a[0] - b[0]).flatMap(([, values]) => values))];
  return {urls, expected: total, visited_pages: [...visited], complete: true, verification: 'counter-drag-v1'};
}


export async function productOperation(api, action, params) {
  if (action === 'product_open') {
    if (!allowedRankingPage(params.url)) throw new Error('page_not_allowed');
    const created = await rankingOperation(api, 'ranking_open', {...params, reuse_workspace: false});
    const session = {requests: new Map(), responses: [], pending: new Set(), phase: 0, bytes: 0, error: ''};
    sessions.set(created.tab_id, session);
    try {
      await api.debugger.sendCommand({tabId: created.tab_id}, 'Network.enable', {maxTotalBufferSize: 16 * 1024 * 1024, maxResourceBufferSize: 8 * 1024 * 1024});
      await rankingOperation(api, 'ranking_navigate', {tab_id: created.tab_id, url: params.url});
      return created;
    } catch (error) {
      sessions.delete(created.tab_id);
      await rankingOperation(api, 'ranking_close', {tab_id: created.tab_id});
      throw error;
    }
  }
  if (!sessions.has(params.tab_id)) throw new Error('product_session_missing');
  const session = sessions.get(params.tab_id);
  if (action === 'product_close') {
    sessions.delete(params.tab_id);
    return await rankingOperation(api, 'ranking_close', params);
  }
  await rankingTarget(api, params.tab_id);
  if (action === 'product_screenshot') return await captureViewport(api, params.tab_id);
  if (action === 'product_dom') {
    if (params.operation === 'images') return await collectGallery(api, params.tab_id);
    const result = await api.scripting.executeScript({target: {tabId: params.tab_id, frameIds: [0]},
      func: productDom, args: [{operation: params.operation, selections: params.selections, name: params.name}]});
    if (!result[0]?.result) throw new Error('product_dom_failed');
    return result[0].result;
  }
  if (action === 'product_reviews_begin') { session.phase++; return {phase: session.phase}; }
  if (action === 'product_network') {
    await Promise.all([...session.pending]);
    if (session.error) throw new Error(session.error);
    if (params.kind === 'reviews') {
      const responses = session.responses.filter(item => item.phase === params.phase && reviewPath(item.url) && Array.isArray(item.data?.data)
        && item.data?.success !== false && [undefined, null, 0, '0'].includes(item.data?.error_code));
      const path = responses[0] ? new URL(responses[0].url).pathname : '';
      const seen = new Set(), comments = [];
      for (const item of responses) {
        if (new URL(item.url).pathname !== path) continue;
        for (const comment of item.data.data) if (comment.review_id != null && !seen.has(String(comment.review_id))) {
          seen.add(String(comment.review_id)); comments.push(comment);
        }
      }
      const exhausted = responses.some(item => {
        const data = item.data;
        const pageSize = Number(item.body?.page_size || item.body?.pageSize || item.body?.size || item.body?.limit || 0);
        return data.data.length === 0 || [data.has_more, data.hasMore, data.has_more_data].some(value => value === false || value === 0)
          || (pageSize > 0 && data.data.length < pageSize);
      });
      return {received: responses.length > 0, comments, exhausted};
    }
    return {responses: session.responses.filter(item => !reviewPath(item.url))};
  }
  throw new Error('unsupported_action');
}
