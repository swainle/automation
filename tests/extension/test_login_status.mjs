import test from "node:test";
import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import vm from "node:vm";

const source = readFileSync(new URL("../../extension/login.js", import.meta.url), "utf8");

test('verification types distinguish slider, SMS, and other identity checks', () => {
  assert.equal(status({body: '拖动滑块完成验证'}).status, 'slider_required');
  assert.equal(status({body: '请输入短信验证码'}).status, 'sms_required');
  assert.equal(status({body: '请完成身份验证'}).status, 'verification_other');
  assert.equal(status({verification: true}).status, 'verification_other');
  assert.equal(status({body: '请输入短信验证码，向右滑动完成验证'}).status, 'slider_required');
});

function status({shop = "", anchors = [], path = "/home", verification = false, loginTab = false, body = ""} = {}) {
  const node = (text, shown = true) => ({innerText: text, children: [],
    getBoundingClientRect: () => ({width: shown ? 100 : 0, height: shown ? 30 : 0}),
    checkVisibility: () => shown});
  const context = {location: {protocol: "https:", hostname: "mms.pinduoduo.com", pathname: path},
    document: {body: {innerText: body},
      querySelector: selector => selector.startsWith("#mms-header-next") && shop ? node(shop) : null,
      querySelectorAll: selector => {
        if (selector === ".captcha-wrapper,.yidun_popup,.yidun_panel,.geetest_panel") return verification ? [node("验证")] : [];
        if (selector === "#mms-main p") return anchors.map(([text, shown]) => node(text, shown));
        if (selector.startsWith("button,a,")) return loginTab ? [node("账号登录")] : [];
        return [];
      }},
    getComputedStyle: () => ({visibility: "visible"})};
  vm.runInNewContext(source, context);
  return JSON.parse(JSON.stringify(context.pddAccountLogin.status()));
}

test("visible receivable area identifies workbench before shop name appears", () => {
  assert.deepEqual(status({anchors: [[" 待收货 ", true]]}), {status: "logged_in", evidence: "receivable_panel"});
});

test("visible shop name still identifies workbench", () => {
  assert.deepEqual(status({shop: "测试店"}), {status: "logged_in", evidence: "shop_name"});
});

test("hidden, duplicated or plain body text cannot identify workbench", () => {
  for (const options of [{anchors: [["待收货", false]]},
    {anchors: [["待收货", true], ["待收货", true]]}, {body: "待收货"}]) {
    assert.equal(status(options).status, "unknown_page");
  }
});

test("receivable fallback does not override verification or login page", () => {
  const anchors = [["待收货", true]];
  assert.equal(status({anchors, verification: true}).status, "verification_other");
  assert.equal(status({anchors, path: "/login", loginTab: true}).status, "login_page");
});
