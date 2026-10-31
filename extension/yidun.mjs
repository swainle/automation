export function allowedYidunPage(value) {
  try {
    const u = new URL(value);
    return (u.origin === "https://dun.163.com" && /^\/trial\/jigsaw\/?$/.test(u.pathname)) ||
      (u.protocol === "http:" && u.hostname === "127.0.0.1" && u.pathname.startsWith("/extension-test/yidun/"));
  } catch { return false; }
}

// 独立序列化到页面的隔离世界，只读取易盾组件。
export async function probeYidun(params = {}) {
  function shown(e) {
    const r = e.getBoundingClientRect(), s = getComputedStyle(e);
    return r.width > 0 && r.height > 0 && s.display !== "none" && s.visibility !== "hidden" && s.opacity !== "0";
  }
  function one(selector, scope = document) {
    if (typeof selector !== "string" || !selector || selector.length > 500) throw new Error("invalid_yidun_selector");
    const found = [...scope.querySelectorAll(selector)].filter(shown);
    if (found.length !== 1) throw new Error("yidun_element_missing_or_ambiguous");
    return found[0];
  }
  function rect(e) {
    const r = e.getBoundingClientRect();
    return {x: r.x, y: r.y, width: r.width, height: r.height};
  }
  try {
    const u = new URL(location.href);
    if (!((u.origin === "https://dun.163.com" && /^\/trial\/jigsaw\/?$/.test(u.pathname)) ||
        (u.protocol === "http:" && u.hostname === "127.0.0.1" && u.pathname.startsWith("/extension-test/yidun/")))) {
      throw new Error("yidun_page_not_allowed");
    }
    if (params.status_only) {
      const root = one(".yidun");
      const text = [...root.querySelectorAll(".yidun_tips__text,.yidun_tips")].filter(shown).map(e => e.textContent).join(" ");
      const passed = /(^|\s)yidun--success(\s|$)/.test(root.className) || /验证成功|验证通过/.test(text);
      const failed = /(^|\s)yidun--error(\s|$)/.test(root.className) || /验证失败|请重试/.test(text);
      return {ok:true, result:{status: passed ? "passed" : failed ? "failed" : "pending"}};
    }
    const buttonSelector = params.button_selector || ".yidun_slider";
    const button = one(buttonSelector);
    const root = button.closest(".yidun") || button.parentElement.parentElement;
    const text = [...root.querySelectorAll(".yidun_tips__text,.yidun_tips")].filter(shown).map(e => e.textContent).join(" ");
    const success = /(^|\s)yidun--success(\s|$)/.test(root.className) || /验证成功|验证通过/.test(text);
    const failed = /(^|\s)yidun--error(\s|$)/.test(root.className) || /验证失败|请重试/.test(text);
    const status = success ? "passed" : failed ? "failed" : "pending";
    if (params.status_only) return {ok: true, result: {status}};
    const selectors = {button_selector: buttonSelector, background_selector: params.background_selector || ".yidun_bg-img",
      piece_selector: params.piece_selector || ".yidun_jigsaw", track_selector: params.track_selector || ".yidun_control"};
    const bg = one(selectors.background_selector), piece = one(selectors.piece_selector), track = one(selectors.track_selector);
    if (![bg, piece].every(e => e instanceof HTMLImageElement)) throw new Error("yidun_requires_image_elements");
    for (const img of [bg, piece]) {
      if (!img.complete) await Promise.race([img.decode(), new Promise((_, reject) => setTimeout(() => reject(new Error("yidun_image_timeout")), 3000))]);
      if (!img.naturalWidth || !img.naturalHeight) throw new Error("yidun_image_not_loaded");
    }
    let image = null, alphaBounds = null;
    function canvas(img) {
      if (img.naturalWidth * img.naturalHeight > 2000000) throw new Error("yidun_image_too_large");
      const c = document.createElement("canvas"); c.width = img.naturalWidth; c.height = img.naturalHeight;
      c.getContext("2d").drawImage(img, 0, 0); return c;
    }
    try { image = canvas(bg).toDataURL("image/png").split(",")[1]; } catch { /* 跨域时由扩展截图回退。 */ }
    try {
      const c = canvas(piece), pixels = c.getContext("2d").getImageData(0, 0, c.width, c.height).data;
      let left = c.width, top = c.height, right = 0, bottom = 0;
      for (let y = 0; y < c.height; y++) for (let x = 0; x < c.width; x++) {
        if (pixels[(y*c.width+x)*4+3] > 128) {left=Math.min(left,x); top=Math.min(top,y); right=Math.max(right,x+1); bottom=Math.max(bottom,y+1);}
      }
      if (right > left) alphaBounds = {left:left/c.width, top:top/c.height, right:right/c.width, bottom:bottom/c.height};
    } catch { /* 无法读透明轮廓时使用元素边界，并记录诊断。 */ }
    const geometry = {background: rect(bg), piece: rect(piece), button: rect(button), track: rect(track)};
    const raw = [bg.currentSrc, piece.currentSrc, image || "", JSON.stringify(geometry)].join("|");
    const hash = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(raw));
    const fingerprint = Array.from(new Uint8Array(hash), b => b.toString(16).padStart(2,"0")).join("");
    return {ok: true, result: {status, selectors, geometry, fingerprint, alpha_bounds: alphaBounds,
      natural_width: bg.naturalWidth, natural_height: bg.naturalHeight,
      image: image ? {format:"png", data:image} : null, image_source: image ? "canvas" : "screenshot"}};
  } catch (error) {
    return {ok: false, error: /^[a-z_]+$/.test(error.message || "") ? error.message : "yidun_probe_failed"};
  }
}

export function easeOutBounce(t) {
  if (t < 1/2.75) return 7.5625*t*t;
  if (t < 2/2.75) {t-=1.5/2.75; return 7.5625*t*t+0.75;}
  if (t < 2.5/2.75) {t-=2.25/2.75; return 7.5625*t*t+0.9375;}
  t-=2.625/2.75; return 7.5625*t*t+0.984375;
}

export async function yidunProbe(api, tabId, params) {
  if (!allowedYidunPage((await api.tabs.get(tabId)).url)) throw new Error("yidun_page_not_allowed");
  const results = await api.scripting.executeScript({target:{tabId, frameIds:[0]}, func:probeYidun, args:[params]});
  const response = results[0]?.result;
  if (!response?.ok) throw new Error(response?.error || "yidun_probe_failed");
  return response.result;
}

export async function dragYidun(api, tabId, params) {
  const distance = params.distance;
  if (!Number.isFinite(distance) || distance <= 0 || distance > 1000 || typeof params.fingerprint !== "string") throw new Error("invalid_yidun_drag");
  const target = {tabId};
  let attached = false, pressed = false;
  let x = 0, y = 0;
  try {
    await api.debugger.attach(target,"1.3"); attached = true;
    const probe = await yidunProbe(api, tabId, params);
    if (probe.fingerprint !== params.fingerprint || probe.status !== "pending") throw new Error("yidun_challenge_changed");
    const {button:b,track:r} = probe.geometry;
    const remaining = r.x+r.width-b.x-b.width;
    if (distance > remaining || b.x < r.x-1 || b.y < 0 || b.x < 0) throw new Error("yidun_distance_out_of_range");
    x=b.x+b.width/2; y=b.y+b.height/2;
    const send = data => api.debugger.sendCommand(target,"Input.dispatchMouseEvent",data);
    await send({type:"mouseMoved", x,y});
    pressed=true;
    await send({type:"mousePressed",x,y,button:"left",buttons:1,clickCount:1});
    await new Promise(resolve=>setTimeout(resolve,150));
    for (let i=1;i<=60;i++) {
      if (!allowedYidunPage((await api.tabs.get(tabId)).url)) throw new Error("yidun_page_changed");
      x=b.x+b.width/2+distance*easeOutBounce(i/60);
      await send({type:"mouseMoved",x,y,button:"left",buttons:1});
      await new Promise(resolve=>setTimeout(resolve,12));
    }
    await new Promise(resolve=>setTimeout(resolve,150));
    return {attempts:1, distance, status:"released"};
  } finally {
    try {
      if (pressed) await api.debugger.sendCommand(target,"Input.dispatchMouseEvent",{type:"mouseReleased",x,y,button:"left",buttons:0,clickCount:1});
    } finally {
      if (attached) await api.debugger.detach(target).catch(()=>{});
    }
  }
}
