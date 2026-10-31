// 拼多多账号登录；不导入 Cookie，不处理验证码，不返回账号密码。
(() => {
  const shown = element => !!element && element.getBoundingClientRect().width > 0 && element.getBoundingClientRect().height > 0 &&
    (element.checkVisibility ? element.checkVisibility({checkOpacity: true, checkVisibilityCSS: true}) : getComputedStyle(element).visibility !== "hidden");
  const fixture = () => location.protocol === "http:" && location.hostname === "127.0.0.1" && location.pathname.startsWith("/extension-test/login/");
  const site = () => location.protocol === "https:" && location.hostname === "mms.pinduoduo.com";
  const loginPage = () => fixture() || (site() && /^\/login\/?$/.test(location.pathname));
  const normalized = element => (element.innerText || element.textContent || "").replace(/\s/g, "");

  function verification() {
    const text = document.body?.innerText || "";
    if ([...document.querySelectorAll(".captcha-wrapper img.slider-img-bg,.yidun_slider,.geetest_slider_button")].some(shown) ||
        /拖动滑块|滑动滑块|滑块验证|向右滑动/.test(text)) return "slider_required";
    if (/短信验证码|手机验证码|手机验证|短信验证/.test(text)) return "sms_required";
    if ([...document.querySelectorAll(".captcha-wrapper,.yidun_popup,.yidun_panel,.geetest_panel")].some(shown) ||
        /身份验证|安全验证|人机验证/.test(text)) return "verification_other";
    return null;
  }

  const verificationResult = () => ({status: verification() || "verification_other", submitted: false});

  function status() {
    if (!site() && !fixture()) throw new Error("login_page_not_allowed");
    const required = verification();
    if (required) return {status: required};
    const text = document.body?.innerText || "";
    const shopName = document.querySelector("#mms-header-next .user-info-top .user-name-name > span");
    if (site() && shown(shopName) && normalized(shopName)) return {status: "logged_in", evidence: "shop_name"};
    // 后台先加载数据区、店铺名稍后显示时，先进入独立的店铺身份核对。
    const receivable = [...document.querySelectorAll("#mms-main p")].filter(node =>
      shown(node) && normalized(node) === "待收货");
    if (site() && !loginPage() && receivable.length === 1) {
      return {status: "logged_in", evidence: "receivable_panel"};
    }
    if (fixture() && document.querySelector("[data-test-login-success]")) return {status: "logged_in"};
    if (/账号或密码错误|账户或密码错误|密码错误|账号不存在|账户不存在|登录失败/.test(text)) return {status: "login_rejected"};
    const form = fields();
    const loginReady = accountTabs().length === 1 || (form.accounts.length === 1 && form.passwords.length === 1);
    return {status: loginPage() && loginReady ? "login_page" : "unknown_page"};
  }

  function fields() {
    const accounts = [...document.querySelectorAll("#usernameId")].filter(shown);
    const passwords = [...document.querySelectorAll("#passwordId")].filter(shown);
    return {accounts, passwords};
  }

  function accountTabs() {
    return [...document.querySelectorAll("button,a,[role=tab],span,div")].filter(e => shown(e) && normalized(e) === "账号登录" &&
      ![...e.children].some(child => shown(child) && normalized(child) === "账号登录"));
  }

  function submitButton(account, password) {
    for (let node = password.parentElement; node && !node.matches("body,html"); node = node.parentElement) {
      if (!node.contains(account)) continue;
      const buttons = [...node.querySelectorAll("button,[role=button],input[type=submit]")].filter(e => shown(e) &&
        (e instanceof HTMLInputElement ? e.value : normalized(e)) === "登录");
      if (buttons.length === 1) return buttons[0];
      if (buttons.length > 1) throw new Error("login_submit_not_unique");
    }
    throw new Error("login_submit_not_found");
  }

  function probe() {
    if (!loginPage()) return status();
    const state = status();
    if (state.status !== "login_page") return state;
    const {accounts, passwords} = fields();
    return {status: "login_page", account_fields: accounts.length, password_fields: passwords.length,
      account_tabs: accountTabs().length, mode: accounts.length === 1 && passwords.length === 1 ? "account" : "needs_account_tab"};
  }

  function fill(input, value) {
    if (!(input instanceof HTMLInputElement) || input.disabled || input.readOnly) throw new Error("login_field_unavailable");
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set.call(input, value);
    input.dispatchEvent(new Event("input", {bubbles: true}));
    input.dispatchEvent(new Event("change", {bubbles: true}));
  }

  async function waitReady(check, failure, timeout = 5000) {
    const deadline = Date.now() + timeout;
    do {
      if (!loginPage()) throw new Error("login_page_changed");
      if (verification()) return null;
      const result = check();
      if (result) return result;
      await new Promise(resolve => setTimeout(resolve, 100));
    } while (Date.now() < deadline);
    throw new Error(failure);
  }

  function editableForm() {
    const form = fields();
    if (form.accounts.length !== 1 || form.passwords.length !== 1) return null;
    if (![form.accounts[0], form.passwords[0]].every(input =>
      input instanceof HTMLInputElement && !input.disabled && !input.readOnly)) return null;
    if (form.passwords[0].type !== "password") throw new Error("password_field_not_masked");
    return form;
  }

  async function submit(params) {
    if (!loginPage()) throw new Error("login_page_not_allowed");
    if (typeof params.account !== "string" || !params.account || typeof params.password !== "string" || !params.password) {
      throw new Error("login_credentials_missing");
    }
    if (verification()) return verificationResult();
    let form = fields();
    if (form.accounts.length !== 1 || form.passwords.length !== 1) {
      const tabs = accountTabs();
      if (tabs.length !== 1) throw new Error("account_login_tab_not_unique");
      tabs[0].click();
      const deadline = Date.now() + 5000;
      do {
        await new Promise(resolve => setTimeout(resolve, 100));
        if (!loginPage()) throw new Error("login_page_changed");
        if (verification()) return verificationResult();
        form = fields();
        if (form.accounts.length === 1 && form.passwords.length === 1) break;
      } while (Date.now() < deadline);
    }
    if (form.accounts.length !== 1 || form.passwords.length !== 1) throw new Error("login_form_not_unique");
    form = await waitReady(editableForm, "login_fields_not_ready");
    if (!form) return verificationResult();
    if (form.passwords[0].type !== "password") throw new Error("password_field_not_masked");
    // 先确认提交按钮属于同一表单，再填写凭据。
    submitButton(form.accounts[0], form.passwords[0]);
    fill(form.accounts[0], params.account);
    const accountReady = await waitReady(() => {
      const current = editableForm();
      return current?.accounts[0].value === params.account ? current : null;
    }, "login_values_not_applied");
    if (!accountReady) return verificationResult();
    if (!loginPage()) throw new Error("login_page_changed");
    form = fields();
    if (form.accounts.length !== 1 || form.passwords.length !== 1 || form.passwords[0].type !== "password") throw new Error("login_form_changed");
    fill(form.passwords[0], params.password);
    // 给受控表单校验一次短缓冲；之后按实际值和按钮状态等待。
    await new Promise(resolve => setTimeout(resolve, 250));
    if (!loginPage()) throw new Error("login_page_changed");
    if (verification()) return verificationResult();
    const button = await waitReady(() => {
      const current = editableForm();
      if (!current || current.accounts[0].value !== params.account || current.passwords[0].value !== params.password) return null;
      const candidate = submitButton(current.accounts[0], current.passwords[0]);
      return candidate.disabled || candidate.getAttribute("aria-disabled") === "true" ? null : candidate;
    }, "login_submit_not_ready");
    if (!button) return verificationResult();
    button.click();
    return {status: "submitted", submitted: true};
  }

  globalThis.pddAccountLogin = {probe, status, submit};
})();
