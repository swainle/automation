// 只读取布局，不修改 DOM、不获取表单值、不执行鼠标操作。
(() => {
  function visible(element) {
    const rect = element.getBoundingClientRect();
    const style = getComputedStyle(element);
    return rect.width > 0 && rect.height > 0 &&
      (element.checkVisibility ? element.checkVisibility({checkOpacity: true, checkVisibilityCSS: true}) :
        style.display !== "none" && style.visibility !== "hidden" && style.opacity !== "0");
  }

  function xpath(element) {
    const parts = [];
    for (let node = element; node && node.nodeType === Node.ELEMENT_NODE; node = node.parentElement) {
      let index = 1;
      for (let sibling = node.previousElementSibling; sibling; sibling = sibling.previousElementSibling) {
        if (sibling.localName === node.localName) index++;
      }
      parts.unshift(`${node.localName}[${index}]`);
    }
    return "/" + parts.join("/");
  }

  function describe(element, kind) {
    const rect = element.getBoundingClientRect();
    const result = {
      kind, tag: element.tagName.toLowerCase(), class_name: element.getAttribute("class") || "",
      xpath: xpath(element), visible: visible(element),
      rect: {x: rect.x, y: rect.y, page_x: rect.x + scrollX, page_y: rect.y + scrollY,
        width: rect.width, height: rect.height}
    };
    if (element instanceof HTMLImageElement) {
      result.natural_width = element.naturalWidth;
      result.natural_height = element.naturalHeight;
    }
    return result;
  }

  function distinct(elements) {
    return elements.filter((item, index) => {
      const a = item.getBoundingClientRect();
      return !elements.slice(0, index).some(other => {
        const b = other.getBoundingClientRect();
        return Math.abs(a.x - b.x) < 2 && Math.abs(a.y - b.y) < 2 &&
          Math.abs(a.width - b.width) < 2 && Math.abs(a.height - b.height) < 2;
      });
    });
  }

  function scan(container, button) {
    const buttonRect = button.getBoundingClientRect();
    const nodes = [container];
    const walker = document.createTreeWalker(container, NodeFilter.SHOW_ELEMENT);
    for (let node = walker.nextNode(); node && nodes.length <= 500; node = walker.nextNode()) nodes.push(node);
    const truncated = nodes.length > 500;
    const nearby = nodes.slice(0, 500).filter(e => e !== button && visible(e));
    const media = distinct(nearby.filter(e => {
      const rect = e.getBoundingClientRect();
      const imageLike = e.matches("img,canvas,svg") || getComputedStyle(e).backgroundImage !== "none";
      return imageLike && rect.width >= 12 && rect.height >= 12 &&
        rect.bottom <= buttonRect.bottom + 12 && rect.bottom >= buttonRect.top - 400;
    }).sort((a, b) => Number(b.matches("img,canvas")) - Number(a.matches("img,canvas"))));
    const backgrounds = media.filter(e => {
      const rect = e.getBoundingClientRect();
      return rect.width >= Math.max(100, buttonRect.width * 2) && rect.height >= 45 &&
        rect.height <= 450 && rect.width <= 1000 && rect.left <= buttonRect.right + 20 &&
        rect.right >= buttonRect.left;
    });
    const tracks = distinct(nearby.filter(e => {
      const rect = e.getBoundingClientRect();
      return !media.includes(e) && rect.width >= buttonRect.width * 1.5 && rect.width <= 1000 &&
        rect.height >= 3 && rect.height <= Math.max(80, buttonRect.height * 2) &&
        rect.left <= buttonRect.left + 5 && rect.right >= buttonRect.right - 5 &&
        Math.abs((rect.top + rect.height / 2) - (buttonRect.top + buttonRect.height / 2)) <= Math.max(25, buttonRect.height / 2);
    })).filter((element, _index, all) => !all.some(other => {
      if (other === element || !other.contains(element)) return false;
      const a = element.getBoundingClientRect(), b = other.getBoundingClientRect();
      // 同一轨道内的进度填充层不应算作第二条轨道。
      return Math.abs(a.left - b.left) < 2 && Math.abs(a.width - b.width) < 2;
    })).filter((element, _index, all) => !all.some(other => {
      if (other === element || !other.contains(element)) return false;
      const a = element.getBoundingClientRect(), b = other.getBoundingClientRect();
      // 同一轨道内的进度填充层不应算作第二条轨道。
      return Math.abs(a.left - b.left) < 2 && Math.abs(a.width - b.width) < 2;
    }));
    const pieces = backgrounds.length === 1 ? media.filter(e => {
      if (e === backgrounds[0]) return false;
      const rect = e.getBoundingClientRect(), bg = backgrounds[0].getBoundingClientRect();
      return rect.width <= bg.width * 0.55 && rect.height <= bg.height + 10 &&
        rect.top >= bg.top - 10 && rect.bottom <= bg.bottom + 10 &&
        rect.left >= bg.left - 10 && rect.right <= bg.right + 10;
    }) : [];
    return {backgrounds, pieces, tracks, media, truncated};
  }

  globalThis.pddDetectCaptcha = params => {
    const hasClass = typeof params.button_class === "string";
    const hasXPath = typeof params.button_xpath === "string";
    if (hasClass === hasXPath) throw new Error("provide_one_button_locator");
    let buttons;
    if (hasClass) {
      const tokens = params.button_class.trim().replace(/^\./, "").split(/\s+/).filter(Boolean);
      if (!tokens.length || params.button_class.length > 1000) throw new Error("invalid_button_class");
      buttons = [...document.querySelectorAll(tokens.map(token => "." + CSS.escape(token)).join(""))];
    } else {
      if (!params.button_xpath.trim() || params.button_xpath.length > 2000) throw new Error("invalid_button_xpath");
      const nodes = document.evaluate(params.button_xpath, document, null, XPathResult.ORDERED_NODE_SNAPSHOT_TYPE, null);
      buttons = Array.from({length: Math.min(nodes.snapshotLength, 100)}, (_, i) => nodes.snapshotItem(i));
      if (nodes.snapshotLength > 100) throw new Error("too_many_button_matches");
    }
    if (buttons.length > 100) throw new Error("too_many_button_matches");
    const shown = buttons.filter(e => e instanceof Element && visible(e));
    const report = {
      status: shown.length ? "ambiguous_button" : "button_not_found", button_matches: buttons.length,
      visible_button_matches: shown.length, buttons: shown.slice(0, 10).map(e => describe(e, "button")),
      container: null, backgrounds: [], pieces: [], tracks: [], inspected_ancestors: [], warnings: [],
      limits: {main_document_only: true, max_ancestors: 8, max_descendants: 500}
    };
    if (shown.length !== 1) {
      report.warnings.push(shown.length ? "可见按钮不唯一，未选择任何按钮" : "主文档中未找到可见按钮；iframe 和 Shadow DOM 暂不支持");
      return report;
    }
    const button = shown[0];
    let chosen = null;
    let ancestor = button.parentElement;
    for (let depth = 1; ancestor && depth <= 8 && !ancestor.matches("body,html"); depth++, ancestor = ancestor.parentElement) {
      const rect = ancestor.getBoundingClientRect();
      if (rect.width > 1100 || rect.height > 900) break;
      const found = scan(ancestor, button);
      report.inspected_ancestors.push({depth, xpath: xpath(ancestor), backgrounds: found.backgrounds.length,
        tracks: found.tracks.length, pieces: found.pieces.length, truncated: found.truncated});
      if (found.backgrounds.length && (!chosen || found.tracks.length)) chosen = {ancestor, ...found};
      // 使用最近的共同容器，不继续扩大到整页，避免混入其他验证码。
      if (found.backgrounds.length && found.tracks.length) break;
    }
    if (!chosen) {
      report.status = "incomplete";
      report.warnings.push("未在限定范围内找到背景图与轨道的共同容器；可能为 iframe、Shadow DOM 或未支持布局");
      return report;
    }
    report.container = describe(chosen.ancestor, "container");
    report.backgrounds = chosen.backgrounds.slice(0, 10).map(e => describe(e, "background"));
    report.pieces = chosen.pieces.slice(0, 10).map(e => describe(e, "piece"));
    report.tracks = chosen.tracks.slice(0, 10).map(e => describe(e, "track"));
    if (chosen.truncated) report.warnings.push("容器内容过多，已限制检查范围，结果可能不完整");
    if (chosen.backgrounds.some(e => e.matches("canvas"))) report.warnings.push("canvas 可能把背景与拼图绘制在同一画布，不能由 DOM 分离拼图");
    const counts = [chosen.backgrounds.length, chosen.pieces.length, chosen.tracks.length];
    report.status = counts.some(n => n > 1) ? "ambiguous" : counts.every(n => n === 1) && !chosen.truncated ? "detected" : "incomplete";
    return report;
  };
})();
