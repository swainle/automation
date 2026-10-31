(() => {
  if (globalThis.__pddDeskReaderInstalled === "0.4.0") return;
  globalThis.__pddDeskReaderInstalled = "0.4.0";
  chrome.runtime.onMessage.addListener((message, sender, respond) => {
    if (sender.id !== chrome.runtime.id || !["element", "captcha_detect", "login_probe", "login_status", "login_submit"].includes(message?.action)) return;
    if (message.action.startsWith("login_")) {
      const action = message.action.slice(6);
      Promise.resolve().then(() => globalThis.pddAccountLogin[action](message.params || {}))
        .then(result => respond({ok: true, result}))
        .catch(error => respond({ok: false, error: /^[a-z_]+$/.test(error.message || "") ? error.message : "login_operation_failed"}));
      return true;
    }
    if (message.action === "captcha_detect") {
      try { respond({ok: true, result: globalThis.pddDetectCaptcha(message.params || {})}); }
      catch { respond({ok: false, error: "captcha_detection_failed"}); }
      return;
    }
    try {
      const {selector, selector_type = "css", limit = 10, include_text = false} = message.params || {};
      if (typeof selector !== "string" || !selector || selector.length > 2000 ||
          !Number.isInteger(limit) || limit < 1 || limit > 20) throw new Error("invalid_selector");
      let elements;
      if (selector_type === "xpath") {
        const snapshot = document.evaluate(selector, document, null, XPathResult.ORDERED_NODE_SNAPSHOT_TYPE, null);
        elements = Array.from({length: Math.min(snapshot.snapshotLength, limit)}, (_, i) => snapshot.snapshotItem(i));
      } else if (selector_type === "css") {
        elements = Array.from(document.querySelectorAll(selector)).slice(0, limit);
      } else { throw new Error("invalid_selector_type"); }
      const result = elements.filter(e => e instanceof Element).map(e => {
        const rect = e.getBoundingClientRect();
        const style = getComputedStyle(e);
        const sensitive = e.matches("input, textarea, select, [contenteditable], [data-private]");
        const item = {
          tag: e.tagName.toLowerCase(), id: e.id || "", class_name: e.getAttribute("class") || "",
          rect: {x: rect.x, y: rect.y, width: rect.width, height: rect.height,
            page_x: rect.x + scrollX, page_y: rect.y + scrollY},
          visible: rect.width > 0 && rect.height > 0 && style.display !== "none" && style.visibility !== "hidden" && style.opacity !== "0"
        };
        // 默认不读文本；即使开启文本也不返回表单值或编辑区域。
        if (include_text && !sensitive) item.text = (e.innerText || "").slice(0, 1000);
        return item;
      });
      respond({ok: true, result: {elements: result, returned: result.length}});
    } catch { respond({ok: false, error: "element_query_failed"}); }
  });
})();
