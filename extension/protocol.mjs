export function allowedPage(value) {
  try {
    const url = new URL(value);
    return (url.protocol === "https:" && ["mms.pinduoduo.com", "dun.163.com"].includes(url.hostname)) ||
      (url.protocol === "http:" && url.hostname === "127.0.0.1" && url.pathname.startsWith("/extension-test/"));
  } catch { return false; }
}

export function validateEndpoint(value) {
  const url = new URL(value);
  if (url.protocol !== "ws:" || url.hostname !== "127.0.0.1" ||
      url.username || url.password || url.search || url.hash || url.pathname !== "/") {
    throw new Error("只允许本机 ws://127.0.0.1:端口 连接");
  }
  return url.href;
}

export function publicTab(tab) {
  const url = new URL(tab.url);
  return {tab_id: tab.id, title: tab.title || "", url: url.origin + url.pathname};
}

export function allowedLoginPage(value, submitting = false) {
  try {
    const url = new URL(value);
    if (url.protocol === "https:" && url.hostname === "mms.pinduoduo.com") {
      return !submitting || /^\/login\/?$/.test(url.pathname);
    }
    return url.protocol === "http:" && url.hostname === "127.0.0.1" && url.pathname.startsWith("/extension-test/login/");
  } catch { return false; }
}
