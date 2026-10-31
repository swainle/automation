import test from "node:test";
import assert from "node:assert/strict";
import {resetState, shopIdentity, tabOperation, closeTaskTabs} from "../../extension/automation.mjs";

function fixture(tabs = [], ids = []) {
  const calls = [];
  const state = {automationTabs: ids};
  const api = {
    storage: {session: {get: async () => state, set: async data => Object.assign(state, data)}},
    tabs: {
      query: async () => tabs,
      get: async id => tabs.find(tab => tab.id === id),
      create: async () => {const tab = {id: 99, url: "about:blank"}; tabs.push(tab); return tab;},
      remove: async ids => {calls.push(["close", ids]); tabs = tabs.filter(tab => ![].concat(ids).includes(tab.id));},
      update: async () => assert.fail("must not navigate"),
    },
    browsingData: {removeLocalStorage: async options => calls.push(["storage", options])},
    cookies: {getAll: async () => {calls.push(["cookies"]); return []; }},
  };
  return {api, calls, state};
}

test("reset closes only registered task tabs before clearing storage and cookies", async () => {
  const {api, calls, state} = fixture([{id: 1, url: "https://mms.pinduoduo.com/home"},
    {id: 2, url: "https://example.com/"}, {id: 3, url: "about:blank"}], [1, 3]);
  const result = await resetState(api);
  assert.deepEqual(calls.slice(0, 2), [["close", [1, 3]], ["storage", {since: 0, origins: ["https://mms.pinduoduo.com"]}]]);
  assert.equal(result.remaining, 0);
  assert.equal(result.local_storage_cleared, true);
  assert.deepEqual(state.automationTabs, []);
});

test("other PDD pages block all clearing without closing user tabs", async () => {
  for (const url of ["https://mms.pinduoduo.com/home", "https://mobile.pinduoduo.com/"]) {
    const {api, calls} = fixture([{id: 1, url}, {id: 2, url: "about:blank"}], [2]);
    await assert.rejects(resetState(api), /other_pdd_tabs_open/);
    assert.deepEqual(calls, []);
  }
});

test("shutdown closes tasks even when another user PDD tab is open", async () => {
  const {api, calls} = fixture([{id: 1, url: "https://mms.pinduoduo.com/home"}, {id: 2, url: "about:blank"}], [2]);
  await closeTaskTabs(api);
  assert.deepEqual(calls, [["close", [2]]]);
});

test("created tabs are registered across background worker restarts", async () => {
  const {api, state} = fixture();
  const result = await tabOperation(api, "tab_create", {});
  assert.equal(result.tab_id, 99);
  assert.deepEqual(state.automationTabs, [99]);
  assert.deepEqual((await closeTaskTabs(api)).closed_tabs, [99]);
});

test("failed cookie deletion prevents successful reset", async () => {
  const {api} = fixture();
  api.cookies = {getAll: async () => [{domain: ".pinduoduo.com", name: "a", value: "private", path: "/", secure: true}], remove: async () => null};
  await assert.rejects(resetState(api), /cookie_clear_incomplete/);
});

test("navigation rejects other domains before updating tab", async () => {
  const {api} = fixture([{id: 1, url: "about:blank"}], [1]);
  await assert.rejects(tabOperation(api, "tab_navigate", {tab_id: 1, url: "https://example.com"}), /page_not_allowed/);
});

test("element-based navigation returns without waiting for page load", async () => {
  const {api} = fixture([{id: 1, url: "about:blank"}], [1]);
  api.tabs.update = async () => ({id: 1, status: "loading"});
  assert.deepEqual(await tabOperation(api, "tab_navigate", {
    tab_id: 1, url: "https://mms.pinduoduo.com/home", wait_for_load: false,
  }), {tab_id: 1, navigating: true});
});

test("unowned tabs cannot be navigated or closed", async () => {
  const {api} = fixture([{id: 1, url: "about:blank"}]);
  await assert.rejects(tabOperation(api, "tab_close", {tab_id: 1}), /tab_not_owned/);
});

test("identity selector must be valid and run only on PDD", async () => {
  const api = {tabs: {get: async () => ({url: "https://example.com"})}};
  await assert.rejects(shopIdentity(api, {tab_id: 1, selector: ".shop"}), /page_not_allowed/);
  await assert.rejects(shopIdentity(api, {tab_id: 1, selector: ""}), /invalid_selector/);
});

test("navigation waits past stale blank complete and pending URL", async () => {
  const {api} = fixture([{id: 1, url: "about:blank"}], [1]);
  const sequence = [
    {id: 1, url: "about:blank", status: "complete"},
    {id: 1, url: "about:blank", status: "complete", pendingUrl: "https://mms.pinduoduo.com/home"},
    {id: 1, url: "https://mms.pinduoduo.com/home", status: "loading"},
    {id: 1, url: "https://mms.pinduoduo.com/home", status: "complete"},
  ];
  let removed = false;
  api.tabs.get = async () => sequence.shift();
  api.tabs.update = async () => ({id: 1, url: "about:blank", status: "complete"});
  api.tabs.onUpdated = {addListener: () => {}, removeListener: () => {removed = true;}};
  assert.deepEqual(await tabOperation(api, "tab_navigate", {tab_id: 1, url: "https://mms.pinduoduo.com/home"}),
    {tab_id: 1, url: "https://mms.pinduoduo.com/home"});
  assert.equal(sequence.length, 0);
  assert.equal(removed, true);
});

test("navigation accepts initially missing blank URL and rejects actual external redirects", async () => {
  const {api} = fixture([], [1]);
  let removed = false;
  const sequence = [{id: 1}, {id: 1, url: "https://example.com/", status: "complete"}];
  api.tabs.get = async () => sequence.shift();
  api.tabs.update = async () => ({status: "loading"});
  api.tabs.onUpdated = {addListener: () => {}, removeListener: () => {removed = true;}};
  await assert.rejects(tabOperation(api, "tab_navigate", {tab_id: 1, url: "https://mms.pinduoduo.com/home"}), /page_not_allowed/);
  assert.equal(removed, true);
});
