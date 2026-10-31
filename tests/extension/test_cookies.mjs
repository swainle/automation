import test from "node:test";
import assert from "node:assert/strict";
import {pddDomain, prepareCookies, cookieOperation} from "../../extension/cookies.mjs";

const sample = {domain: ".pinduoduo.com", name: "session", value: "fixture", path: "/", secure: true, httpOnly: true};
function mockApi() {
  const state = {items: [{...sample, storeId: "0"}, {...sample, domain: "other.test"}], calls: []};
  return Object.assign(state, {
    async getAll() { return state.items; },
    async remove(details) {
      state.calls.push(["remove", details]);
      state.items = state.items.filter(c => !(c.name === details.name && pddDomain(c.domain)));
      return details;
    },
    async set(details) { state.calls.push(["set", details]); state.items.push({...sample, ...details}); return details; }
  });
}
test("root and subdomains only, including leading dot", () => {
  for (const domain of ["pinduoduo.com", ".pinduoduo.com", "mobile.pinduoduo.com"]) assert.ok(pddDomain(domain));
  for (const domain of ["evilpinduoduo.com", "pinduoduo.com.evil.test", "..pinduoduo.com", "pinduoduo.com/", null]) assert.equal(pddDomain(domain), false);
});
test("invalid import never clears existing cookies", async () => {
  for (const raw of [[sample, {...sample, domain: "other.test"}], [{...sample, expires: "invalid"}], [{...sample, path: "invalid"}], [{...sample, sameSite: "invalid"}]]) {
    const api = mockApi();
    await assert.rejects(cookieOperation(api, "cookies_import", {cookies: raw}));
    assert.equal(api.calls.length, 0);
  }
});
test("export filters other sites; import removes before writing; clear is idempotent", async () => {
  const api = mockApi();
  assert.equal((await cookieOperation(api, "cookies_export")).count, 1);
  const result = await cookieOperation(api, "cookies_import", {cookies: [sample]});
  assert.equal(result.cleared, 1);
  assert.equal(result.imported, 1);
  assert.deepEqual(api.calls.map(c => c[0]), ["remove", "set"]);
  assert.ok(api.items.some(c => c.domain === "other.test"));
  await cookieOperation(api, "cookies_clear");
  assert.equal((await cookieOperation(api, "cookies_clear")).cleared, 0);
});
test("preserves host-only, HttpOnly, session, expiry, SameSite and partition metadata", () => {
  const [host] = prepareCookies([{...sample, domain: "mobile.pinduoduo.com", hostOnly: true, sameSite: "None", session: true}]);
  assert.equal(host.domain, undefined);
  assert.equal(host.httpOnly, true);
  assert.equal(host.expirationDate, undefined);
  assert.equal(host.sameSite, "no_restriction");
  const key = {topLevelSite: "https://pinduoduo.com", hasCrossSiteAncestor: true};
  const [domain] = prepareCookies({cookies: [{...sample, expires: Date.now()/1000 + 3600, partitionKey: key}]});
  assert.equal(domain.domain, ".pinduoduo.com");
  assert.ok(domain.expirationDate);
  assert.deepEqual(domain.partitionKey, key);
});
test("failed clear stops import", async () => {
  const api = mockApi();
  api.remove = async () => null;
  await assert.rejects(cookieOperation(api, "cookies_import", {cookies: [sample]}), /cookie_clear_failed/);
  assert.equal(api.calls.length, 0);
});

test("clear returns only successful identities without cookie values", async () => {
  const result = await cookieOperation(mockApi(), "cookies_clear");
  assert.deepEqual(result.cleared_cookies, [{domain: sample.domain, name: sample.name, path: sample.path}]);
  assert.deepEqual(result.failed_cookies, []);
  assert.equal(result.remaining, 0);
  assert.equal(JSON.stringify(result).includes(sample.value), false);
});
test("clear reports null and rejected removals separately from successes", async () => {
  for (const reject of [false, true]) {
    const api = mockApi();
    api.remove = async () => { if (reject) throw new Error("private value"); return null; };
    const result = await cookieOperation(api, "cookies_clear");
    assert.equal(result.cleared, 0);
    assert.deepEqual(result.cleared_cookies, []);
    assert.deepEqual(result.failed_cookies, [{domain: sample.domain, name: sample.name, path: sample.path, error: "cookie_clear_failed"}]);
    assert.equal(result.remaining, 1);
    assert.equal(JSON.stringify(result).includes("private value"), false);
  }
});

test("expired cookies retain expiration and are submitted even when API returns null", async () => {
  const api = mockApi();
  api.set = async details => { api.calls.push(["set", details]); return null; };
  const result = await cookieOperation(api, "cookies_import", {cookies: [{...sample, expires: 1}]});
  assert.equal(api.calls.find(call => call[0] === "set")[1].expirationDate, 1);
  assert.equal(result.submitted, 1);
  assert.equal(result.remaining, 0);
});
test("empty cookie backup validates and restores empty state", async () => {
  const api = mockApi();
  assert.deepEqual(await cookieOperation(api, "cookies_validate", {cookies: []}), {count: 0});
  const result = await cookieOperation(api, "cookies_import", {cookies: []});
  assert.equal(result.submitted, 0);
  assert.equal(result.remaining, 0);
});
