import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';

const source = readFileSync(new URL('../../extension/login.js', import.meta.url), 'utf8');

test('login waits for editable fields and enabled submit, then clicks exactly once', async () => {
  let clicks = 0, ticks = 0, now = 0;
  class Input {
    constructor(type) { this.type = type; this.disabled = true; this.readOnly = false; }
    get value() { return this.stored || ''; }
    set value(value) { this.stored = value; }
    dispatchEvent() {}
    getBoundingClientRect() { return {width: 100, height: 30}; }
    checkVisibility() { return true; }
  }
  const account = new Input('text'), password = new Input('password');
  const button = {disabled: true, innerText: '登录', getAttribute: () => null,
    getBoundingClientRect: () => ({width: 100, height: 30}), checkVisibility: () => true,
    click() { clicks++; }};
  const parent = {parentElement: null, matches: () => false, contains: () => true,
    querySelectorAll: () => [button]};
  account.parentElement = password.parentElement = parent;
  const context = {HTMLInputElement: Input, Event: class {}, Date: {now: () => now},
    location: {protocol: 'https:', hostname: 'mms.pinduoduo.com', pathname: '/login'},
    document: {body: {innerText: ''}, querySelectorAll(selector) {
      return selector === '#usernameId' ? [account] : selector === '#passwordId' ? [password] : [];
    }}, setTimeout(callback, delay) {
      now += delay; ticks++;
      if (ticks >= 2) account.disabled = password.disabled = false;
      if (ticks >= 5) button.disabled = false;
      callback();
    }};
  vm.runInNewContext(source, context);
  const result = await context.pddAccountLogin.submit({account: 'fixture', password: 'fixture-secret'});
  assert.equal(result.submitted, true);
  assert.equal(clicks, 1);
  assert.ok(ticks >= 5);
  assert.equal(account.value, 'fixture');
  assert.equal(password.value, 'fixture-secret');
});
