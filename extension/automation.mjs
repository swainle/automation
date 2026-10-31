import {STORAGE_ORIGIN} from "./local-storage.mjs";
import {cookieOperation} from "./cookies.mjs";

function pddPage(value) {
  try {
    const url = new URL(value);
    return url.origin === STORAGE_ORIGIN && !url.username && !url.password;
  } catch { return false; }
}

async function ownedIds(api) {
  return (await api.storage.session.get("automationTabs")).automationTabs || [];
}

function cookiePage(tab) {
  try { return /(^|\.)pinduoduo\.com$/i.test(new URL(tab.pendingUrl || tab.url).hostname); }
  catch { return false; }
}

async function assertNoOtherPddTabs(api) {
  const owned = new Set(await ownedIds(api));
  if ((await api.tabs.query({})).some(tab => cookiePage(tab) && !owned.has(tab.id))) {
    throw new Error("other_pdd_tabs_open");
  }
}

export async function closeTaskTabs(api) {
  const owned = new Set(await ownedIds(api));
  const tabs = (await api.tabs.query({})).filter(tab => owned.has(tab.id));
  if (tabs.length) await api.tabs.remove(tabs.map(tab => tab.id));
  await api.storage.session.set({automationTabs: []});
  return {closed_tabs: tabs.map(tab => tab.id)};
}

export async function tabOperation(api, action, params) {
  if (action === "tab_create") {
    const tab = await api.tabs.create({url: "about:blank", active: true});
    await api.storage.session.set({automationTabs: [...await ownedIds(api), tab.id]});
    return {tab_id: tab.id};
  }
  if (!Number.isInteger(params.tab_id)) throw new Error("invalid_tab_id");
  if (!(await ownedIds(api)).includes(params.tab_id)) throw new Error("tab_not_owned");
  let tab = await api.tabs.get(params.tab_id);
  if (tab.url && tab.url !== "about:blank" && !pddPage(tab.url)) throw new Error("page_not_allowed");
  if (action === "tab_close") {
    await api.tabs.remove(tab.id);
    await api.storage.session.set({automationTabs: (await ownedIds(api)).filter(id => id !== tab.id)});
    return {closed: true};
  }
  if (action !== "tab_navigate") throw new Error("unsupported_action");
  if (!pddPage(params.url)) throw new Error("page_not_allowed");
  if (params.wait_for_load === false) {
    await api.tabs.update(tab.id, {url: params.url});
    return {tab_id: tab.id, navigating: true};
  }
  const initialUrl = tab.url;
  let started = false;
  const changed = (id, change) => {
    if (id === params.tab_id && (change.status === "loading" || change.url)) started = true;
  };
  api.tabs.onUpdated?.addListener(changed);
  try {
    const updated = await api.tabs.update(tab.id, {url: params.url});
    started ||= updated?.status === "loading" || Boolean(updated?.pendingUrl);
    const deadline = Date.now() + 30000;
    while (Date.now() < deadline) {
      tab = await api.tabs.get(params.tab_id);
      // Chrome 可能仍返回旧空白页 complete；必须等到目标导航真正提交。
      if (tab.status === "loading" || tab.pendingUrl) started = true;
      if (tab.url && tab.url !== "about:blank") {
        if (!pddPage(tab.url)) throw new Error("page_not_allowed");
        if (tab.status === "complete" && !tab.pendingUrl && (started || tab.url !== initialUrl)) {
          const url = new URL(tab.url);
          return {tab_id: tab.id, url: url.origin + url.pathname};
        }
      }
      await new Promise(resolve => setTimeout(resolve, 100));
    }
    throw new Error("page_load_timeout");
  } finally {
    api.tabs.onUpdated?.removeListener(changed);
  }
}

// 只关闭插件登记的任务页；其他拼多多页面存在时不清除共享登录状态。
export async function resetState(api) {
  await assertNoOtherPddTabs(api);
  const {closed_tabs} = await closeTaskTabs(api);
  await assertNoOtherPddTabs(api);
  await api.browsingData.removeLocalStorage({since: 0, origins: [STORAGE_ORIGIN]});
  await assertNoOtherPddTabs(api);
  const cookies = await cookieOperation(api.cookies, "cookies_clear");
  if (cookies.remaining || cookies.failed_cookies.length) throw new Error("cookie_clear_incomplete");
  return {remaining: 0, local_storage_cleared: true, closed_tabs};
}

export async function shopIdentity(api, params) {
  if (!Number.isInteger(params.tab_id) || typeof params.selector !== "string" ||
      !params.selector || params.selector.length > 2000) throw new Error("invalid_selector");
  if (!pddPage((await api.tabs.get(params.tab_id)).url)) throw new Error("page_not_allowed");
  const results = await api.scripting.executeScript({
    target: {tabId: params.tab_id, frameIds: [0]},
    func: (origin, selector) => {
      if (location.origin !== origin) throw new Error("page_not_allowed");
      const nodes = [...document.querySelectorAll(selector)].filter(node =>
        !node.matches("input,textarea,select,[contenteditable]") &&
        node.getBoundingClientRect().width > 0 && node.getBoundingClientRect().height > 0);
      if (nodes.length !== 1) return {shop_name: null};
      return {shop_name: (nodes[0].innerText || "").trim()};
    },
    args: [STORAGE_ORIGIN, params.selector]
  });
  return results[0]?.result ?? {shop_name: null};
}
