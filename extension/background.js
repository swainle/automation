import {allowedPage, allowedLoginPage, validateEndpoint, publicTab} from "./protocol.mjs";
import {yidunProbe, dragYidun} from "./yidun.mjs";
import {cookieOperation} from "./cookies.mjs";
import {openStoragePage, storageOperation} from "./local-storage.mjs";
import {tabOperation, resetState, shopIdentity, closeTaskTabs} from "./automation.mjs";
import {receivableOperation} from "./receivable.mjs";
import {screenshotUiOperation} from "./screenshot-ui.mjs";

let socket = null;
let heartbeat = null;
let reconnect = null;
let generation = 0;
let serial = Promise.resolve();

async function status(state) {
  await chrome.storage.session.set({bridgeStatus: state});
}

async function targetTab(params) {
  if (Number.isInteger(params.tab_id)) {
    const tab = await chrome.tabs.get(params.tab_id);
    if (!allowedPage(tab.url)) throw new Error("page_not_allowed");
    return tab;
  }
  const tabs = (await chrome.tabs.query({})).filter(t => allowedPage(t.url));
  if (tabs.length !== 1) throw new Error("select_tab_id_required");
  return tabs[0];
}

async function element(tab, params, action = "element") {
  const message = {action, params};
  try {
    return await chrome.tabs.sendMessage(tab.id, message, {frameId: 0});
  } catch {
    await chrome.scripting.executeScript({target: {tabId: tab.id, frameIds: [0]}, files: ["captcha-detect.js", "login.js", "content.js"]});
    return await chrome.tabs.sendMessage(tab.id, message, {frameId: 0});
  }
}

async function screenshot(tab, params) {
  const target = {tabId: tab.id};
  let attached = false;
  try {
    // 截图由扩展 API 执行，Python/Playwright 不获取页面截图。
    await chrome.debugger.attach(target, "1.3");
    attached = true;
    const metrics = await chrome.debugger.sendCommand(target, "Page.getLayoutMetrics");
    let clip;
    if (params.selector) {
      const query = await element(tab, {...params, limit: 2, include_text: false});
      if (!query.ok || query.result.elements.length !== 1 || !query.result.elements[0].visible) {
        throw new Error("screenshot_requires_one_visible_element");
      }
      const rect = query.result.elements[0].rect;
      if (params.viewport_to_bottom) {
        const view = metrics.cssLayoutViewport;
        clip = {x: view.pageX, y: view.pageY, width: view.clientWidth,
          height: rect.page_y + rect.height - view.pageY, scale: 1};
      } else {
        clip = {x: rect.page_x, y: rect.page_y, width: rect.width, height: rect.height, scale: 1};
      }
    } else if (params.full_page) {
      const size = metrics.cssContentSize;
      clip = {x: 0, y: 0, width: size.width, height: size.height, scale: 1};
    } else {
      const view = metrics.cssLayoutViewport;
      clip = {x: view.pageX, y: view.pageY, width: view.clientWidth, height: view.clientHeight, scale: 1};
    }
    if (![clip.x, clip.y, clip.width, clip.height].every(Number.isFinite) || clip.x < 0 || clip.y < 0 ||
        clip.width <= 0 || clip.height <= 0 || clip.width * clip.height > 25000000) {
      throw new Error("screenshot_area_invalid_or_too_large");
    }
    // 导航可能发生在任务处理中，截图前重新确认目标页面。
    if (!allowedPage((await chrome.tabs.get(tab.id)).url)) throw new Error("page_not_allowed");
    const format = params.format === "jpeg" ? "jpeg" : "png";
    const image = await chrome.debugger.sendCommand(target, "Page.captureScreenshot", {
      format, ...(format === "jpeg" ? {quality: 95} : {}), captureBeyondViewport: true, clip
    });
    if (!image.data || image.data.length > 22000000) throw new Error("screenshot_too_large");
    return {format, data: image.data, width: clip.width, height: clip.height, origin_x: clip.x, origin_y: clip.y};
  } finally {
    if (attached) await chrome.debugger.detach(target).catch(() => {});
  }
}

async function execute(message) {
  if (!message || typeof message.id !== "string" || message.id.length > 100) throw new Error("invalid_request");
  const params = message.params || {};
  if (["screenshot_prepare", "screenshot_restore"].includes(message.action)) return await screenshotUiOperation(chrome, message.action, params);
  if (["tab_create", "tab_navigate", "tab_close"].includes(message.action)) return await tabOperation(chrome, message.action, params);
  if (message.action === "automation_tabs_close") return await closeTaskTabs(chrome);
  if (["browser_state_clear", "automation_state_clear"].includes(message.action)) return await resetState(chrome, params);
  if (message.action === "shop_identity") return await shopIdentity(chrome, params);
  if (["notification_close", "receivable_panel", "receivable_amount"].includes(message.action)) return await receivableOperation(chrome, message.action, params);
  if (message.action === "local_storage_open") return await openStoragePage(chrome, params);
  if (["local_storage_import", "local_storage_export", "local_storage_clear"].includes(message.action)) {
    return await storageOperation(chrome, message.action, params);
  }
  if (["cookies_export", "cookies_import", "cookies_clear", "cookies_validate"].includes(message.action)) {
    return await cookieOperation(chrome.cookies, message.action, params);
  }
  if (message.action === "tabs") {
    return {tabs: (await chrome.tabs.query({})).filter(t => allowedPage(t.url)).map(publicTab)};
  }
  const tab = await targetTab(params);
  if (["yidun_probe", "yidun_status", "yidun_drag"].includes(message.action)) {
    if (message.action === "yidun_drag") return await dragYidun(chrome, tab.id, params);
    const result = await yidunProbe(chrome, tab.id, {...params, status_only: message.action === "yidun_status"});
    if (message.action === "yidun_probe" && !result.image) {
      result.image = await screenshot(tab, {selector: result.selectors.background_selector});
      const fresh = await yidunProbe(chrome, tab.id, params);
      if (fresh.fingerprint !== result.fingerprint) throw new Error("yidun_challenge_changed");
    }
    return result;
  }
  if (["login_probe", "login_status", "login_submit"].includes(message.action)) {
    if (!allowedLoginPage(tab.url, message.action === "login_submit")) throw new Error("login_page_not_allowed");
    let response;
    if (message.action === "login_submit") {
      // 可重试的预检查不携带凭据；提交命令始终只发送一次。
      await element(tab, {selector: "body", limit: 1});
      if (!allowedLoginPage((await chrome.tabs.get(tab.id)).url, true)) throw new Error("login_page_not_allowed");
      try { response = await chrome.tabs.sendMessage(tab.id, {action: message.action, params}, {frameId: 0}); }
      catch { throw new Error("login_submission_outcome_unknown"); }
    } else {
      response = await element(tab, {}, message.action);
    }
    if (!response?.ok) throw new Error(response?.error || "login_operation_failed");
    return response.result;
  }
  if (message.action === "captcha_detect") {
    const query = await element(tab, params, "captcha_detect");
    if (!query?.ok) throw new Error("captcha_detection_failed");
    const result = query.result;
    try {
      result.screenshot = await screenshot(tab, result.container ? {
        selector: result.container.xpath, selector_type: "xpath"
      } : {});
    } catch { result.screenshot_error = "diagnostic_screenshot_failed"; }
    return result;
  }
  if (message.action === "element") {
    const result = await element(tab, params);
    if (!result?.ok) throw new Error("element_query_failed");
    return result.result;
  }
  if (message.action === "screenshot") return await screenshot(tab, params);
  throw new Error("unsupported_action");
}

async function connect() {
  const current = ++generation;
  clearTimeout(reconnect);
  clearInterval(heartbeat);
  if (socket) { socket.onclose = null; socket.close(); socket = null; }
  const config = await chrome.storage.local.get(["endpoint"]);
  if (current !== generation) return;
  let endpoint;
  try { endpoint = validateEndpoint(config.endpoint || "ws://127.0.0.1:8765/"); }
  catch { await status("连接地址无效，仅允许 127.0.0.1"); return; }
  const ws = new WebSocket(endpoint);
  socket = ws;
  ws.onopen = () => ws.send(JSON.stringify({type: "hello", role: "extension"}));
  ws.onmessage = event => {
    let message;
    try { message = JSON.parse(event.data); } catch { return; }
    if (message.type === "hello") {
      if (message.ok) {
        void status("已连接");
        heartbeat = setInterval(() => {
          if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({type: "ping"}));
        }, 20000);
      } else { void status("连接握手失败"); }
      return;
    }
    if (message.type !== "request") return;
    // 顺序处理，避免两个截图任务同时附加 debugger。
    serial = serial.catch(() => {}).then(async () => {
      let response;
      try { response = {type: "result", id: message.id, ok: true, result: await execute(message)}; }
      catch (error) {
        const known = /^[a-z_]+$/.test(error.message || "") ? error.message : "extension_operation_failed";
        response = {type: "result", id: message.id, ok: false, error: known};
      }
      if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(response));
    });
  };
  ws.onerror = () => { void status("连接失败，等待本机服务"); };
  ws.onclose = () => {
    clearInterval(heartbeat);
    if (current === generation) {
      void status("连接已断开，正在重连");
      reconnect = setTimeout(() => { void connect(); }, 3000);
    }
  };
}

chrome.action.onClicked.addListener(() => { void chrome.runtime.openOptionsPage(); });
chrome.runtime.onInstalled.addListener(() => { void connect(); });
chrome.runtime.onStartup.addListener(() => { void connect(); });
chrome.storage.onChanged.addListener((changes, area) => {
  if (area === "local" && changes.endpoint) void connect();
});
chrome.alarms.onAlarm.addListener(alarm => {
  if (alarm.name === "bridge-reconnect" && (!socket || socket.readyState === WebSocket.CLOSED)) void connect();
});
void chrome.alarms.create("bridge-reconnect", {periodInMinutes: 1});
void connect();
