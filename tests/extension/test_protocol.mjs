import test from "node:test";
import assert from "node:assert/strict";
import {allowedPage, allowedLoginPage, validateEndpoint, publicTab} from "../../extension/protocol.mjs";

test("only supported pages are allowed", () => {
  assert.equal(allowedPage("https://mms.pinduoduo.com/home/"), true);
  assert.equal(allowedPage("https://dun.163.com/"), true);
  assert.equal(allowedPage("https://dun.163.com/trial/jigsaw"), true);
  assert.equal(allowedPage("http://127.0.0.1:1234/extension-test/index.html"), true);
  for (const value of ["https://mms.pinduoduo.com.evil.test/", "https://dun.163.com.evil.test/", "https://other.163.com/", "http://dun.163.com/", "chrome://settings", "file:///secret", "http://127.0.0.1/private"]) {
    assert.equal(allowedPage(value), false);
  }
});
test("bridge address must remain local without query parameters", () => {
  assert.equal(validateEndpoint("ws://127.0.0.1:8765"), "ws://127.0.0.1:8765/");
  for (const value of ["ws://example.com/", "ws://127.0.0.1:8765/?token=bad", "ws://user:pass@127.0.0.1:8765/"]) {
    assert.throws(() => validateEndpoint(value));
  }
});
test("tab listing strips URL query secrets", () => {
  assert.deepEqual(publicTab({id: 3, title: "店铺", url: "https://mms.pinduoduo.com/home/?secret=abc#token"}),
    {tab_id: 3, title: "店铺", url: "https://mms.pinduoduo.com/home/"});
});

test("account submission is limited to Pinduoduo login page", () => {
  assert.equal(allowedLoginPage("https://mms.pinduoduo.com/login/", true), true);
  assert.equal(allowedLoginPage("https://mms.pinduoduo.com/home/"), true);
  for (const value of ["https://mms.pinduoduo.com/home/", "https://dun.163.com/", "https://mms.pinduoduo.com.evil.test/login/", "http://mms.pinduoduo.com/login/"]) {
    assert.equal(allowedLoginPage(value, true), false);
  }
});
