import test from "node:test";
import assert from "node:assert/strict";
import {storageUrl, pageStorageOperation, STORAGE_ORIGIN, openStoragePage} from "../../extension/local-storage.mjs";

test("storage URLs are restricted to merchant HTTPS origin", () => {
  assert.equal(storageUrl(`${STORAGE_ORIGIN}/home`), `${STORAGE_ORIGIN}/home`);
  for (const url of ["http://mms.pinduoduo.com", "https://mms.pinduoduo.com.evil.test", "https://mobile.pinduoduo.com", "https://mms.pinduoduo.com:444", "https://user@mms.pinduoduo.com"]) assert.throws(() => storageUrl(url));
});
function fixture() {
  const items = new Map([["old", "private-value"]]);
  globalThis.location = {origin: STORAGE_ORIGIN};
  globalThis.window = {localStorage: {
    get length() { return items.size; }, key(i) { return [...items.keys()][i]; },
    getItem(key) { return items.get(key) ?? null; },
    setItem(key, value) { items.set(key, value); }, clear() { items.clear(); }
  }};
  return items;
}
test("invalid payload and origin changes do not clear storage", () => {
  const items = fixture();
  for (const data of [null, [], {key: 1}]) assert.equal(pageStorageOperation(STORAGE_ORIGIN, "local_storage_import", data).ok, false);
  globalThis.location.origin = "https://other.test";
  assert.equal(pageStorageOperation(STORAGE_ORIGIN, "local_storage_clear").ok, false);
  assert.equal(items.get("old"), "private-value");
});
test("roundtrip, special keys and clearing disclose keys only", () => {
  const items = fixture();
  const data = JSON.parse('{"__proto__":"fixture","name":"value","unicode":"中文"}');
  const imported = pageStorageOperation(STORAGE_ORIGIN, "local_storage_import", data);
  assert.equal(imported.result.imported, 3);
  assert.equal(items.has("old"), false);
  assert.deepEqual(Object.fromEntries(pageStorageOperation(STORAGE_ORIGIN, "local_storage_export").result.local_storage_entries), data);
  const cleared = pageStorageOperation(STORAGE_ORIGIN, "local_storage_clear");
  assert.deepEqual(cleared.result.cleared_keys, Object.keys(data));
  assert.equal(cleared.result.remaining, 0);
  assert.equal(JSON.stringify(cleared).includes('"value"'), false);
  assert.equal(pageStorageOperation(STORAGE_ORIGIN, "local_storage_clear").result.cleared, 0);
});
test("quota errors are explicit and never expose values", () => {
  fixture();
  window.localStorage.setItem = () => { throw new Error("sensitive value"); };
  assert.deepEqual(pageStorageOperation(STORAGE_ORIGIN, "local_storage_import", {key: "value"}), {ok: false, error: "local_storage_operation_partial"});
});
test("opens missing page and reuses existing page; multiple pages require selection", async () => {
  let created = 0;
  const tab = {id: 1, url: `${STORAGE_ORIGIN}/home`, status: "complete"};
  const api = {tabs: {query: async () => [], create: async () => {created++; return tab;}, get: async () => tab}};
  assert.equal((await openStoragePage(api, {url: tab.url})).tab_id, 1);
  assert.equal(created, 1);
  api.tabs.query = async () => [tab];
  await openStoragePage(api, {url: tab.url});
  assert.equal(created, 1);
  api.tabs.query = async () => [tab, {...tab, id: 2}];
  await assert.rejects(openStoragePage(api, {url: tab.url}), /select_tab_id_required/);
});
