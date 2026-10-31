// 以“待收货”为锚点，条框和金额均从锚点相对定位。
export function probeNotification(click = true) {
  if (location.origin !== "https://mms.pinduoduo.com") throw new Error("page_not_allowed");
  const visible = node => {
    const rect = node.getBoundingClientRect();
    if (rect.width <= 0 || rect.height <= 0) return false;
    for (let current = node; current; current = current.parentElement) {
      const style = getComputedStyle(current);
      if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    }
    return true;
  };
  // 不依赖 body/div[5] 的序号，仅清理已确认的通知或发货地提示。
  for (const modal of document.querySelectorAll('[class*="MDL_innerWrapper_"],[role="dialog"],.ant-modal,.next-dialog')) {
    const text = (modal.innerText || '').replace(/\s/g, '');
    const appeal = text.includes('今日平台已支持您以下申诉') && text.includes('打款成功');
    const shipping = text.startsWith('请选择您的主要发货地');
    const appealPrompt = /^现有\d+笔订单[，,]?您可一键发起维权申诉[！!]?/.test(text);
    if (!visible(modal) || (!appeal && !shipping && !appealPrompt)) continue;
    const buttons = [...modal.querySelectorAll('[class*="MDL_iconWrapper_"],.ant-modal-close,.next-dialog-close,[aria-label="Close"],[aria-label="关闭"]')].filter(visible);
    if (buttons.length !== 1) throw new Error(shipping ? 'shipping_notification_close_not_unique' : 'appeal_notification_close_not_unique');
    if (click) buttons[0].click();
    return {visible: true, clicked: click};
  }
  const icon = document.evaluate('//*[@id="umd-msgbox-handler"]/div[1]/div/i', document,
    null, XPathResult.FIRST_ORDERED_NODE_TYPE, null).singleNodeValue;
  if (!icon) return {visible: false};
  // i 图标可能本身没有尺寸，点击区域由父元素提供。
  let target = icon;
  while (target) {
    const style = getComputedStyle(target);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return {visible: false};
    if (target.id === 'umd-msgbox-handler') break;
    target = target.parentElement;
  }
  target = icon;
  while (target && target.id !== 'umd-msgbox-handler') {
    const rect = target.getBoundingClientRect();
    if (rect.width > 0 && rect.height > 0) {
      if (click) icon.click();
      return {visible: true, clicked: click};
    }
    target = target.parentElement;
  }
  return {visible: false};
}

export async function closeNotification(api, tabId) {
  const deadline = Date.now() + 10000;
  const appearUntil = Date.now() + 1500;
  let clicked = false;
  let lastClick = 0;
  while (Date.now() < deadline) {
    const results = await api.scripting.executeScript({target: {tabId, frameIds: [0]},
      func: probeNotification, args: [Date.now() - lastClick >= 500]});
    const result = results[0]?.result;
    if (!result || typeof result.visible !== 'boolean') throw new Error('notification_probe_failed');
    if (result.clicked) { clicked = true; lastClick = Date.now(); }
    if (!result.visible && (clicked || Date.now() >= appearUntil)) return {closed: true, clicked};
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  throw new Error('notification_close_timeout');
}

export function probeReceivable(reveal = false) {
  if (location.origin !== "https://mms.pinduoduo.com") throw new Error("page_not_allowed");
  const visible = node => {
    if (!node) return false;
    const rect = node.getBoundingClientRect();
    const style = getComputedStyle(node);
    return rect.width > 0 && rect.height > 0 && style.display !== "none" &&
      style.visibility !== "hidden" && style.opacity !== "0";
  };
  const text = node => (node?.innerText || node?.textContent || "").replace(/\s/g, "");
  const one = (xpath, context = document) =>
    document.evaluate(xpath, context, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null).singleNodeValue;
  const notification = one('//*[@id="umd-msgbox-handler"]/div[1]/div/i');
  if (visible(notification)) {
    notification.click();
    return {ready: false};
  }
  let anchor = one('//*[@id="mms-main"]/div[8]/div/div/div/div[4]/p[1]');
  if (!visible(anchor) || text(anchor) !== "待收货") {
    const candidates = [...document.querySelectorAll("#mms-main p")].filter(node =>
      visible(node) && text(node) === "待收货");
    if (candidates.length !== 1) return {ready: false};
    anchor = candidates[0];
  }
  const panel = one('ancestor::*[parent::*[@id="mms-main"]][1]', anchor);
  if (!visible(panel)) return {ready: false};
  const index = [...panel.parentElement.children].indexOf(panel) + 1;
  const selector = `//*[@id="mms-main"]/*[${index}]`;
  const amountNode = one("../p[3]/span", anchor);
  const raw = text(amountNode);
  const match = raw.match(/^(?:待到账[：:]?)?[¥￥]?((?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{1,2})?)(?:元)?$/);
  if (visible(amountNode) && match) return {ready: true, selector, amount: match[1].replace(/,/g, "")};
  if (reveal && visible(amountNode) && /^(?:待到账[：:]?)?查看金额$/.test(raw)) {
    const button = one('//*[@id="mms-main"]/div[8]/div[1]/div/div/div/div[4]/p[3]/span/span/span') ||
      one('./span/span', amountNode) || amountNode;
    button.click();
    return {ready: true, selector, clicked: true};
  }
  return {ready: true, selector};
}

export async function receivableOperation(api, action, params) {
  if (!Number.isInteger(params.tab_id)) throw new Error("invalid_tab_id");
  const tab = await api.tabs.get(params.tab_id);
  if (new URL(tab.url).origin !== "https://mms.pinduoduo.com") throw new Error("page_not_allowed");
  const notification = await closeNotification(api, tab.id);
  if (action === 'notification_close') return notification;
  if (action === "receivable_panel") {
    await api.windows.update(tab.windowId, {state: "maximized", focused: true});
    await api.tabs.update(tab.id, {active: true});
    await api.scripting.executeScript({target: {tabId: tab.id, frameIds: [0]}, func: async () => {
      window.scrollTo(0, 0);
      await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
    }});
  }
  const deadline = Date.now() + 10000;
  let clicked = false;
  while (Date.now() < deadline) {
    const results = await api.scripting.executeScript({target: {tabId: tab.id, frameIds: [0]},
      func: probeReceivable, args: [action === "receivable_amount" && !clicked]});
    const result = results[0]?.result;
    clicked ||= Boolean(result?.clicked);
    if (action === "receivable_panel" ? result?.ready : result?.amount !== undefined) return result;
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  throw new Error(action === "receivable_panel" ? "receivable_panel_not_found" : "receivable_amount_not_found");
}
