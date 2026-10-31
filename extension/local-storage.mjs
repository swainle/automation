export const STORAGE_ORIGIN = "https://mms.pinduoduo.com";

export function storageUrl(value) {
  let url;
  try { url = new URL(value); } catch { throw new Error("invalid_storage_url"); }
  if (url.origin !== STORAGE_ORIGIN || url.username || url.password) throw new Error("storage_origin_not_allowed");
  return url.href;
}

export async function openStoragePage(api, params) {
  const url = storageUrl(params.url);
  const matches = (await api.tabs.query({})).filter(tab => {
    try { return new URL(tab.url).origin === STORAGE_ORIGIN; } catch { return false; }
  });
  let tab;
  if (params.tab_id !== undefined) {
    if (!Number.isInteger(params.tab_id)) throw new Error("invalid_tab_id");
    tab = await api.tabs.get(params.tab_id);
    try { storageUrl(tab.url); } catch { throw new Error("storage_origin_not_allowed"); }
  } else {
    if (matches.length > 1) throw new Error("select_tab_id_required");
    tab = matches[0] ?? await api.tabs.create({url, active: true});
  }
  const deadline = Date.now() + 15000;
  while (Date.now() < deadline) {
    tab = await api.tabs.get(tab.id);
    if (tab.status === "complete") {
      storageUrl(tab.url);
      return {tab_id: tab.id, origin: STORAGE_ORIGIN};
    }
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  throw new Error("storage_page_load_timeout");
}

// 此函数序列化后在主框架中运行，不依赖扩展模块作用域。
export function pageStorageOperation(origin, action, data) {
  if (location.origin !== origin) return {ok: false, error: "storage_origin_changed"};
  if (!["local_storage_import", "local_storage_export", "local_storage_clear"].includes(action)) {
    return {ok: false, error: "unsupported_action"};
  }
  if (action === "local_storage_import" && typeof data === "string") {
    try { data = JSON.parse(data); } catch { return {ok: false, error: "invalid_local_storage"}; }
  }
  if (action === "local_storage_import" && (!data || typeof data !== "object" || Array.isArray(data) ||
      Object.values(data).some(value => typeof value !== "string"))) {
    return {ok: false, error: "invalid_local_storage"};
  }
  let modified = false;
  try {
    const storage = window.localStorage;
    const keys = Array.from({length: storage.length}, (_, i) => storage.key(i));
    if (action === "local_storage_export") {
      return {ok: true, result: {local_storage_entries: keys.map(key => [key, storage.getItem(key)]), count: keys.length}};
    }
    modified = true;
    storage.clear();
    if (action === "local_storage_import") {
      for (const [key, value] of Object.entries(data)) storage.setItem(key, value);
      if (Object.entries(data).some(([key, value]) => storage.getItem(key) !== value)) {
        return {ok: false, error: "local_storage_import_incomplete"};
      }
    }
    return {ok: true, result: {cleared: keys.length, cleared_keys: keys,
      imported: action === "local_storage_import" ? Object.keys(data).length : 0, remaining: storage.length}};
  } catch {
    return {ok: false, error: modified ? "local_storage_operation_partial" : "local_storage_unavailable"};
  }
}

export async function storageOperation(api, action, params) {
  if (!Number.isInteger(params.tab_id)) throw new Error("invalid_tab_id");
  storageUrl((await api.tabs.get(params.tab_id)).url);
  // JSON 字符串和键值对数组避免 Chrome 注入参数/结果转换丢失 __proto__ 等键。
  const results = await api.scripting.executeScript({target: {tabId: params.tab_id, frameIds: [0]},
    func: pageStorageOperation, args: [STORAGE_ORIGIN, action, JSON.stringify(params.local_storage ?? null)]});
  const response = results[0]?.result;
  if (!response?.ok) throw new Error(response?.error ?? "local_storage_operation_failed");
  if (action === "local_storage_export") {
    response.result.local_storage = Object.fromEntries(response.result.local_storage_entries);
    delete response.result.local_storage_entries;
  }
  return {...response.result, tab_id: params.tab_id, origin: STORAGE_ORIGIN};
}
