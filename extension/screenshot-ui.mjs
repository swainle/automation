// 仅处理安全守护安装推广，不隐藏业务或验证码提示。
export async function screenshotUi(restore = false) {
  if (location.origin !== 'https://mms.pinduoduo.com') throw new Error('page_not_allowed');
  const key = '__automationScreenshotBanner';
  const previous = globalThis[key];
  if (restore) {
    for (const item of previous || []) {
      if (!item.node.isConnected) continue;
      if (item.value) item.node.style.setProperty('display', item.value, item.priority);
      else item.node.style.removeProperty('display');
    }
    delete globalThis[key];
    await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
    return {restored: true};
  }
  if (previous) return {hidden: true};
  const visible = node => {
    const rect = node.getBoundingClientRect(), style = getComputedStyle(node);
    return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
  };
  // 展开层可能自身高度为 0，但其溢出的子节点仍可见，必须一并隐藏。
  const known = [...document.querySelectorAll('.mms-main-safe-tip,.mms-main-safe-content')].filter(node => {
    const text = (node.innerText || node.textContent || '').replace(/\s/g, '');
    return text.includes('安全守护') && text.includes('安装') &&
      !node.querySelector('#mms-main,input[type="password"],.captcha-wrapper');
  });
  if (known.length) {
    globalThis[key] = known.map(node => ({node, value: node.style.getPropertyValue('display'),
      priority: node.style.getPropertyPriority('display')}));
    for (const node of known) node.style.setProperty('display', 'none', 'important');
    await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
    if (known.some(node => getComputedStyle(node).display !== 'none')) throw new Error('screenshot_banner_still_visible');
    return {hidden: true, found: true, count: known.length};
  }
  const candidates = [...document.querySelectorAll('div,section,aside')].filter(node => {
    if (!visible(node)) return false;
    const rect = node.getBoundingClientRect();
    const text = (node.innerText || '').replace(/\s/g, '');
    return rect.top >= 0 && rect.top < 200 && rect.height <= 250 && rect.width >= innerWidth * .7 &&
      text.length < 300 && text.includes('安全守护') && text.includes('安装') &&
      !node.querySelector('#mms-main,input[type="password"],.captcha-wrapper');
  }).sort((a, b) => a.getBoundingClientRect().height - b.getBoundingClientRect().height);
  const node = candidates[0];
  if (!node) return {hidden: false, found: false};
  const close = [...node.querySelectorAll('button,[role="button"],[aria-label], [title]')].find(button =>
    visible(button) && /^(关闭|关闭横幅|关闭推广|close|×)$/i.test(
      (button.getAttribute('aria-label') || button.getAttribute('title') || button.innerText || '').trim()));
  if (close) {
    close.click();
    await new Promise(resolve => setTimeout(resolve, 300));
    if (!node.isConnected || !visible(node)) return {closed: true, found: true};
  }
  globalThis[key] = [{node, value: node.style.getPropertyValue('display'), priority: node.style.getPropertyPriority('display')}];
  node.style.setProperty('display', 'none', 'important');
  await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
  if (visible(node)) throw new Error('screenshot_banner_still_visible');
  return {hidden: true, found: true};
}

export async function screenshotUiOperation(api, action, params) {
  if (!Number.isInteger(params.tab_id)) throw new Error('invalid_tab_id');
  if (new URL((await api.tabs.get(params.tab_id)).url).origin !== 'https://mms.pinduoduo.com') throw new Error('page_not_allowed');
  const results = await api.scripting.executeScript({target: {tabId: params.tab_id, frameIds: [0]},
    func: screenshotUi, args: [action === 'screenshot_restore']});
  return results[0]?.result;
}
