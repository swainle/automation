// Cookie 操作始终限定在当前扩展上下文的拼多多 Cookie store。
export function pddDomain(domain) {
  return typeof domain === "string" && /^\.?([a-z0-9-]+\.)*pinduoduo\.com$/i.test(domain);
}

function cookieUrl(cookie) {
  const host = cookie.domain.replace(/^\./, "");
  const url = new URL(`${cookie.secure ? "https" : "http"}://${host}/`);
  // 防止路径被解释为 query、fragment 或另一个主机。
  url.pathname = cookie.path;
  return url.href;
}

export function prepareCookies(raw) {
  const items = Array.isArray(raw) ? raw : raw?.cookies;
  if (!Array.isArray(items)) throw new Error("invalid_cookie_file");
  return items.map(item => {
    if (!item || !pddDomain(item.domain) || typeof item.name !== "string" ||
        typeof item.value !== "string") throw new Error("invalid_cookie_entry");
    const cookie = {...item, path: item.path ?? "/", secure: item.secure ?? false};
    if (typeof cookie.path !== "string" || !cookie.path.startsWith("/") ||
        /[\x00-\x20\x7f;=]/.test(cookie.name) || /[\x00-\x1f\x7f;]/.test(cookie.value) ||
        /[\x00-\x1f\x7f]/.test(cookie.path)) throw new Error("invalid_cookie_entry");
    for (const key of ["secure", "httpOnly", "hostOnly", "session", "sessionCookie"]) {
      if (item[key] !== undefined && typeof item[key] !== "boolean") throw new Error("invalid_cookie_entry");
    }
    const details = {url: cookieUrl(cookie), name: cookie.name, value: cookie.value,
      path: cookie.path, secure: cookie.secure, httpOnly: item.httpOnly ?? false};
    if (!(item.hostOnly ?? !item.domain.startsWith("."))) details.domain = item.domain;
    const sameSites = {strict: "strict", lax: "lax", none: "no_restriction",
      no_restriction: "no_restriction", unspecified: "unspecified"};
    const sameSiteKey = String(item.sameSite ?? "unspecified").toLowerCase();
    const sameSite = Object.hasOwn(sameSites, sameSiteKey) ? sameSites[sameSiteKey] : null;
    if (!sameSite || (sameSite === "no_restriction" && !cookie.secure)) throw new Error("invalid_cookie_entry");
    details.sameSite = sameSite;
    const expires = item.expirationDate ?? item.expires;
    if (!item.session && !item.sessionCookie && expires !== undefined && expires !== -1) {
      if (!Number.isFinite(expires)) throw new Error("invalid_cookie_expiration");
      details.expirationDate = expires;
    }
    if (item.partitionKey !== undefined) {
      const key = item.partitionKey;
      if (!key || typeof key.topLevelSite !== "string" || !cookie.secure) throw new Error("invalid_cookie_partition");
      let site;
      try { site = new URL(key.topLevelSite); } catch { throw new Error("invalid_cookie_partition"); }
      if (!["http:", "https:"].includes(site.protocol) || site.origin !== key.topLevelSite ||
          (key.hasCrossSiteAncestor !== undefined && typeof key.hasCrossSiteAncestor !== "boolean")) {
        throw new Error("invalid_cookie_partition");
      }
      details.partitionKey = {topLevelSite: key.topLevelSite};
      if (key.hasCrossSiteAncestor !== undefined) details.partitionKey.hasCrossSiteAncestor = key.hasCrossSiteAncestor;
    }
    if (cookie.name.startsWith("__Secure-") && !cookie.secure) throw new Error("invalid_cookie_entry");
    if (cookie.name.startsWith("__Host-") && (!cookie.secure || details.domain || cookie.path !== "/")) {
      throw new Error("invalid_cookie_entry");
    }
    return details;
  });
}

export async function cookieOperation(api, action, params = {}) {
  const list = async () => (await api.getAll({domain: "pinduoduo.com"})).filter(c => pddDomain(c.domain));
  if (action === "cookies_validate") return {count: prepareCookies(params.cookies).length};
  if (action === "cookies_export") {
    const cookies = await list();
    return {cookies, count: cookies.length};
  }
  // 在任何删除发生前，完整校验所有导入条目。
  const prepared = action === "cookies_import" ? prepareCookies(params.cookies) : null;
  if (action !== "cookies_clear" && !prepared) throw new Error("unsupported_action");
  let cleared = 0;
  const clearedCookies = [];
  const failedCookies = [];
  for (const cookie of await list()) {
    const details = {url: cookieUrl(cookie), name: cookie.name, storeId: cookie.storeId};
    if (cookie.partitionKey) details.partitionKey = cookie.partitionKey;
    const summary = {domain: cookie.domain, name: cookie.name, path: cookie.path};
    try {
      if (!await api.remove(details)) throw new Error("cookie_clear_failed");
      cleared++;
      clearedCookies.push(summary);
    } catch {
      if (action !== "cookies_clear") throw new Error("cookie_clear_failed");
      failedCookies.push({...summary, error: "cookie_clear_failed"});
    }
  }
  if (action === "cookies_clear") {
    return {cleared, imported: 0, remaining: (await list()).length,
      cleared_cookies: clearedCookies, failed_cookies: failedCookies};
  }
  if ((await list()).length) throw new Error("cookie_clear_incomplete");
  let imported = 0;
  for (const details of prepared ?? []) {
    const result = await api.set(details);
    // 过期 Cookie 仍按原有效期提交；浏览器可能立即删除并返回空结果。
    if (!result && !(details.expirationDate !== undefined && details.expirationDate <= Date.now() / 1000)) {
      throw new Error("cookie_import_failed");
    }
    imported++;
  }
  return {cleared, imported, submitted: imported, remaining: (await list()).length};
}
