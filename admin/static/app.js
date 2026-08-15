document.addEventListener("submit", (event) => {
  const form = event.target;
  if (form.dataset.confirm && !window.confirm(form.dataset.confirm)) {
    event.preventDefault();
  }
});

(function () {
  const body = document.body;
  const toggle = document.getElementById("navToggle");
  const backdrop = document.getElementById("sidebarBackdrop");
  if (!toggle || !body.classList.contains("app-body")) return;

  function setOpen(open) {
    body.classList.toggle("nav-open", open);
    if (backdrop) backdrop.hidden = !open;
  }

  toggle.addEventListener("click", () => setOpen(!body.classList.contains("nav-open")));
  if (backdrop) backdrop.addEventListener("click", () => setOpen(false));
})();

/** Auto-logout after 2 hours of no admin activity; stay logged in while the tab is used. */
(function () {
  if (!document.body.classList.contains("app-body")) return;

  const IDLE_MS = 2 * 60 * 60 * 1000;
  const PING_EVERY_MS = 5 * 60 * 1000;
  const CHECK_EVERY_MS = 30 * 1000;
  let lastActiveAt = Date.now();
  let lastPingAt = 0;

  function markActive() {
    lastActiveAt = Date.now();
  }

  ["mousemove", "mousedown", "keydown", "touchstart", "scroll", "click", "visibilitychange"].forEach(
    (eventName) => {
      document.addEventListener(
        eventName,
        () => {
          if (eventName === "visibilitychange" && document.visibilityState !== "visible") return;
          markActive();
        },
        { passive: true }
      );
    }
  );

  async function pingSession() {
    try {
      const res = await fetch("/admin/session/ping", {
        credentials: "same-origin",
        headers: { Accept: "application/json" },
      });
      if (!res.ok) {
        window.location.href = "/admin/logout";
        return;
      }
      const data = await res.json();
      if (!data || data.logged_in === false) {
        window.location.href =
          "/admin/login?error=" +
          encodeURIComponent("Session expired after 2 hours of inactivity. Please log in again.");
      }
    } catch (_) {
      /* network blip — server will enforce on next navigation */
    }
  }

  setInterval(() => {
    const idleFor = Date.now() - lastActiveAt;
    if (idleFor >= IDLE_MS) {
      window.location.href = "/admin/logout";
      return;
    }
    // While the admin is actively using this device/tab, refresh the server session.
    if (idleFor < PING_EVERY_MS && Date.now() - lastPingAt >= PING_EVERY_MS) {
      lastPingAt = Date.now();
      pingSession();
    }
  }, CHECK_EVERY_MS);
})();

/** Keep admin sidebar scrolled to the same place across page navigations. */
(function () {
  const nav = document.querySelector(".sidebar-nav");
  if (!nav) return;

  const KEY = "smf-admin-sidebar-scroll";

  function saveScroll() {
    try {
      sessionStorage.setItem(KEY, String(nav.scrollTop));
    } catch (_) {
      /* private mode / blocked storage */
    }
  }

  function restoreScroll() {
    let restored = false;
    try {
      const saved = sessionStorage.getItem(KEY);
      if (saved !== null && saved !== "") {
        const top = parseInt(saved, 10);
        if (!Number.isNaN(top)) {
          nav.scrollTop = top;
          restored = true;
        }
      }
    } catch (_) {
      /* ignore */
    }

    const active = nav.querySelector("a.active");
    if (!active) return;

    // If the active link is off-screen after restore (or no saved position),
    // nudge just enough to show it — never jump to the top of the nav.
    const navRect = nav.getBoundingClientRect();
    const linkRect = active.getBoundingClientRect();
    const offscreen =
      linkRect.bottom > navRect.bottom - 4 || linkRect.top < navRect.top + 4;
    if (!restored || offscreen) {
      active.scrollIntoView({ block: "nearest", inline: "nearest" });
    }
    saveScroll();
  }

  restoreScroll();
  // Layout / fonts can shift heights slightly after first paint.
  requestAnimationFrame(restoreScroll);
  window.addEventListener("load", restoreScroll);

  nav.addEventListener("scroll", saveScroll, { passive: true });
  nav.querySelectorAll("a[href]").forEach((link) => {
    link.addEventListener("click", saveScroll);
  });
})();

/** Insert premium <tg-emoji> tags into textareas / inputs. */
function insertAtCursor(field, text) {
  if (!field) return;
  const start = field.selectionStart ?? field.value.length;
  const end = field.selectionEnd ?? field.value.length;
  const before = field.value.slice(0, start);
  const after = field.value.slice(end);
  field.value = before + text + after;
  const pos = start + text.length;
  field.focus();
  if (typeof field.setSelectionRange === "function") {
    field.setSelectionRange(pos, pos);
  }
  field.dispatchEvent(new Event("input", { bubbles: true }));
}

function escapeHtmlText(value) {
  return String(value || "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function stripZwsp(value) {
  return String(value || "").replace(/\u200B/g, "");
}

function isIconField(field, surface) {
  if (field && field.classList.contains("emoji-icon-source")) return true;
  if (surface && surface.getAttribute("data-mode") === "icon") return true;
  return false;
}

function parseIconCombined(value) {
  const str = String(value || "").trim();
  const match = str.match(/^(\d+)\|(.*)$/);
  if (!match) return null;
  return { id: match[1], fallback: match[2] || "✨" };
}

function premiumImgHtml(id, fallback) {
  const fb = fallback || "✨";
  return (
    `<img class="emoji-inline-thumb" src="/admin/custom-emoji/${id}" alt="${escapeHtmlText(fb)}" ` +
    `data-emoji-id="${id}" data-fallback="${escapeHtmlText(fb)}" draggable="false">`
  );
}

function sourceToSurfaceHtml(source, { iconMode = false } = {}) {
  const str = String(source || "");
  if (iconMode) {
    const icon = parseIconCombined(str);
    if (icon) return premiumImgHtml(icon.id, icon.fallback) + "\u200B";
    return escapeHtmlText(str);
  }
  const re = /<tg-emoji\s+emoji-id="(\d+)">([^<]*)<\/tg-emoji>/gi;
  let out = "";
  let last = 0;
  let match;
  while ((match = re.exec(str))) {
    out += escapeHtmlText(str.slice(last, match.index)).replace(/\n/g, "<br>");
    out += premiumImgHtml(match[1], match[2] || "✨");
    out += "\u200B";
    last = match.index + match[0].length;
  }
  out += escapeHtmlText(str.slice(last)).replace(/\n/g, "<br>");
  return out;
}

function surfaceToSource(surface) {
  if (!surface) return "";
  const field = document.getElementById(surface.getAttribute("data-for") || "");
  const iconMode = isIconField(field, surface);
  let out = "";

  function walk(node) {
    if (node.nodeType === Node.TEXT_NODE) {
      out += stripZwsp(node.nodeValue || "");
      return;
    }
    if (node.nodeType !== Node.ELEMENT_NODE) return;
    const tag = node.tagName;
    if (tag === "IMG" && node.dataset && node.dataset.emojiId) {
      const fb = node.dataset.fallback || "✨";
      if (iconMode) {
        out = `${node.dataset.emojiId}|${fb}`;
        return;
      }
      out += `<tg-emoji emoji-id="${node.dataset.emojiId}">${fb}</tg-emoji>`;
      return;
    }
    if (tag === "BR") {
      out += "\n";
      return;
    }
    const isBlock = tag === "DIV" || tag === "P";
    if (isBlock && out && !out.endsWith("\n")) out += "\n";
    Array.from(node.childNodes).forEach(walk);
    if (isBlock && out && !out.endsWith("\n")) out += "\n";
  }

  if (iconMode) {
    const img = surface.querySelector("img.emoji-inline-thumb[data-emoji-id]");
    if (img) {
      return `${img.dataset.emojiId}|${img.dataset.fallback || "✨"}`;
    }
    return stripZwsp(surface.textContent || "").trim();
  }

  Array.from(surface.childNodes).forEach(walk);
  return out.replace(/\n+$/, "");
}

function findRichSurfaceFor(field) {
  if (!field || !field.id) return null;
  return document.querySelector(`.emoji-rich-surface[data-for="${field.id}"]`);
}

function syncSurfaceToSource(surface) {
  if (!surface) return;
  const field = document.getElementById(surface.getAttribute("data-for") || "");
  if (!field) return;
  const next = surfaceToSource(surface);
  if (field.value === next) return;
  field.value = next;
  // Mark sync so page scripts can ignore echo "input" events if needed.
  field.dataset.emojiSync = "1";
  field.dispatchEvent(new Event("input", { bubbles: true }));
  delete field.dataset.emojiSync;
}

function bindImgFallback(img) {
  if (!img || img.dataset.fbBound === "1") return;
  img.dataset.fbBound = "1";
  img.addEventListener("error", () => {
    const fb = img.getAttribute("data-fallback") || img.alt || "✨";
    img.replaceWith(document.createTextNode(fb));
  });
}

function syncSourceToSurface(field) {
  const surface = findRichSurfaceFor(field);
  if (!surface) return;
  const iconMode = isIconField(field, surface);
  surface.innerHTML = sourceToSurfaceHtml(field.value || "", { iconMode });
  surface.querySelectorAll("img.emoji-inline-thumb").forEach(bindImgFallback);
}

function placeCaretAtEnd(el) {
  if (!el) return;
  const range = document.createRange();
  range.selectNodeContents(el);
  range.collapse(false);
  const sel = window.getSelection();
  if (!sel) return;
  sel.removeAllRanges();
  sel.addRange(range);
}

function placeCaretAfterNode(node) {
  if (!node || !node.parentNode) return;
  let zw = node.nextSibling;
  if (!(zw && zw.nodeType === Node.TEXT_NODE && zw.nodeValue && zw.nodeValue.indexOf("\u200B") === 0)) {
    zw = document.createTextNode("\u200B");
    node.parentNode.insertBefore(zw, node.nextSibling);
  }
  const range = document.createRange();
  range.setStart(zw, Math.min(1, zw.nodeValue.length));
  range.collapse(true);
  const sel = window.getSelection();
  if (!sel) return;
  sel.removeAllRanges();
  sel.addRange(range);
}

/** Remember caret inside a rich surface so ✨ panel clicks don't lose insert position. */
const savedSurfaceRanges = new WeakMap();

function saveSurfaceSelection(surface) {
  if (!surface) return;
  const sel = window.getSelection();
  if (!sel || !sel.rangeCount) return;
  const range = sel.getRangeAt(0);
  if (!surface.contains(range.commonAncestorContainer)) return;
  try {
    savedSurfaceRanges.set(surface, range.cloneRange());
  } catch (_) {
    /* ignore */
  }
}

function restoreSurfaceSelection(surface) {
  if (!surface) return false;
  const saved = savedSurfaceRanges.get(surface);
  if (!saved) return false;
  try {
    // Range may be stale after innerHTML rebuilds — verify nodes still attached.
    if (!surface.contains(saved.startContainer) && saved.startContainer !== surface) {
      return false;
    }
    const sel = window.getSelection();
    if (!sel) return false;
    sel.removeAllRanges();
    sel.addRange(saved);
    return true;
  } catch (_) {
    return false;
  }
}

function makePremiumImg(emojiId, fallback) {
  const img = document.createElement("img");
  img.className = "emoji-inline-thumb";
  img.src = "/admin/custom-emoji/" + emojiId;
  img.alt = fallback || "✨";
  img.dataset.emojiId = emojiId;
  img.dataset.fallback = fallback || "✨";
  img.draggable = false;
  bindImgFallback(img);
  return img;
}

function insertPremiumImgAtCursor(surface, emojiId, fallback, { replace = false } = {}) {
  if (!surface) return;
  surface.focus();
  restoreSurfaceSelection(surface);
  const img = makePremiumImg(emojiId, fallback);

  if (replace) {
    surface.innerHTML = "";
    surface.appendChild(img);
    placeCaretAfterNode(img);
    saveSurfaceSelection(surface);
    syncSurfaceToSource(surface);
    return;
  }

  const sel = window.getSelection();
  if (sel && sel.rangeCount && surface.contains(sel.anchorNode)) {
    const range = sel.getRangeAt(0);
    range.deleteContents();
    range.insertNode(img);
    placeCaretAfterNode(img);
  } else {
    // No saved caret — append after existing text, never prepend at start.
    surface.appendChild(img);
    placeCaretAfterNode(img);
  }
  saveSurfaceSelection(surface);
  syncSurfaceToSource(surface);
}

function caretEmojiNeighbor(surface, direction) {
  const sel = window.getSelection();
  if (!sel || !sel.rangeCount || !surface.contains(sel.anchorNode)) return null;
  const range = sel.getRangeAt(0);
  if (!range.collapsed) return null;

  let node = range.startContainer;
  let offset = range.startOffset;

  if (node.nodeType === Node.TEXT_NODE) {
    const text = node.nodeValue || "";
    if (direction < 0) {
      const before = stripZwsp(text.slice(0, offset));
      if (before.length > 0) return null;
      node = node.previousSibling;
    } else {
      const after = stripZwsp(text.slice(offset));
      if (after.length > 0) return null;
      node = node.nextSibling;
    }
  } else if (node.nodeType === Node.ELEMENT_NODE) {
    if (direction < 0) {
      node = offset > 0 ? node.childNodes[offset - 1] : node.previousSibling;
    } else {
      node = offset < node.childNodes.length ? node.childNodes[offset] : node.nextSibling;
    }
  } else {
    return null;
  }

  while (node && node.nodeType === Node.TEXT_NODE && stripZwsp(node.nodeValue || "") === "") {
    node = direction < 0 ? node.previousSibling : node.nextSibling;
  }
  if (node && node.nodeType === Node.ELEMENT_NODE && node.tagName === "IMG" && node.dataset.emojiId) {
    return node;
  }
  return null;
}

function enhanceRichEmojiField(field) {
  if (!field || field.dataset.richEnhanced === "1") return;
  const surface = findRichSurfaceFor(field);
  if (!surface) return;
  field.dataset.richEnhanced = "1";
  field.classList.add("emoji-rich-source");
  if (surface.getAttribute("data-mode") === "icon") {
    field.classList.add("emoji-icon-source");
  }
  syncSourceToSurface(field);

  surface.addEventListener("input", () => {
    syncSurfaceToSource(surface);
    saveSurfaceSelection(surface);
  });
  surface.addEventListener("keyup", () => saveSurfaceSelection(surface));
  surface.addEventListener("mouseup", () => saveSurfaceSelection(surface));
  surface.addEventListener("blur", () => {
    saveSurfaceSelection(surface);
    syncSurfaceToSource(surface);
  });

  // Stop label activation from yanking focus, but do NOT preventDefault —
  // that was resetting the caret to the start of the field.
  surface.addEventListener("mousedown", (event) => {
    event.stopPropagation();
  });
  surface.addEventListener("click", (event) => {
    event.stopPropagation();
    saveSurfaceSelection(surface);
  });

  surface.addEventListener("keydown", (event) => {
    if (event.key !== "Backspace" && event.key !== "Delete") return;
    const direction = event.key === "Backspace" ? -1 : 1;
    const img = caretEmojiNeighbor(surface, direction);
    if (!img) return;
    event.preventDefault();
    const zw = img.nextSibling;
    const prev = img.previousSibling;
    img.remove();
    if (zw && zw.nodeType === Node.TEXT_NODE && stripZwsp(zw.nodeValue || "") === "") {
      zw.remove();
    }
    syncSurfaceToSource(surface);
    if (prev && prev.parentNode) {
      placeCaretAfterNode(prev.nodeType === Node.ELEMENT_NODE ? prev : prev);
    } else {
      placeCaretAtEnd(surface);
    }
    saveSurfaceSelection(surface);
  });

  surface.addEventListener("paste", (event) => {
    event.preventDefault();
    const text = (event.clipboardData || window.clipboardData)?.getData("text") || "";
    document.execCommand("insertText", false, text);
    syncSurfaceToSource(surface);
    saveSurfaceSelection(surface);
  });

  const form = field.form;
  if (form && form.dataset.richSyncBound !== "1") {
    form.dataset.richSyncBound = "1";
    form.addEventListener("submit", () => {
      form.querySelectorAll(".emoji-rich-surface").forEach((el) => syncSurfaceToSource(el));
    });
  }
}

function enhanceAllRichEmojiFields(root) {
  (root || document).querySelectorAll("input.emoji-rich-source, textarea.emoji-rich-source").forEach(enhanceRichEmojiField);
  (root || document).querySelectorAll(".emoji-rich-surface[data-for]").forEach((surface) => {
    const field = document.getElementById(surface.getAttribute("data-for") || "");
    if (field) enhanceRichEmojiField(field);
  });
}

function findTextPickerTarget(select) {
  if (!select) return null;
  if (select.dataset.target) {
    return document.querySelector(select.dataset.target);
  }
  if (!select.form) return null;
  return (
    select.form.querySelector("textarea[name='description']") ||
    select.form.querySelector("textarea[name='message']") ||
    select.form.querySelector("input[name='description']") ||
    select.form.querySelector("input[name='name']")
  );
}

function applyEmojiTextSelection(select, optionOrValue, fallback) {
  const target = findTextPickerTarget(select);
  if (!target) return false;

  let emojiId = "";
  let fb = fallback || "✨";
  if (typeof optionOrValue === "string") {
    emojiId = optionOrValue;
    const icon = parseIconCombined(emojiId);
    if (icon) {
      emojiId = icon.id;
      fb = icon.fallback || fb;
    }
  } else if (optionOrValue) {
    emojiId = optionOrValue.value;
    fb = optionOrValue.dataset?.fallback || fb;
    const icon = parseIconCombined(emojiId);
    if (icon) {
      emojiId = icon.id;
      fb = icon.fallback || fb;
    }
  }
  if (!emojiId) return false;
  // Prefer numeric id from data-emoji-id when value is combined.
  if (!/^\d+$/.test(String(emojiId)) && optionOrValue && optionOrValue.dataset?.emojiId) {
    emojiId = optionOrValue.dataset.emojiId;
  }
  if (!/^\d+$/.test(String(emojiId))) return false;

  const surface = findRichSurfaceFor(target);
  const iconMode =
    isIconField(target, surface) ||
    select.dataset.iconMode === "1" ||
    select.getAttribute("data-icon-mode") === "1";

  if (surface) {
    enhanceRichEmojiField(target);
    // Icon-only fields replace the single icon. Text/description fields always
    // INSERT at the caret so multiple premium emojis (same line or many lines) stay.
    const multiline = Boolean(surface.closest(".emoji-rich-multiline"));
    insertPremiumImgAtCursor(surface, emojiId, fb, { replace: iconMode && !multiline });
    resetPickerUI(select);
    closeEmojiKbdPanel(select);
    surface.focus();
    return true;
  }

  if (iconMode) {
    target.value = `${emojiId}|${fb}`;
    target.dispatchEvent(new Event("input", { bubbles: true }));
    resetPickerUI(select);
    closeEmojiKbdPanel(select);
    target.focus();
    return true;
  }

  const tag = `<tg-emoji emoji-id="${emojiId}">${fb}</tg-emoji>`;
  insertAtCursor(target, tag);
  resetPickerUI(select);
  closeEmojiKbdPanel(select);
  target.focus();
  return true;
}

const iconPresetTargets = new WeakMap();

function findIconPresetTarget(select) {
  if (!select) return null;

  const cached = iconPresetTargets.get(select);
  if (cached && document.contains(cached)) return cached;

  if (select.dataset.target) {
    const target = document.querySelector(select.dataset.target);
    if (target) return target;
  }

  const wrap = select.closest(".emoji-search-wrap");
  if (wrap) {
    const beforeWrap = wrap.previousElementSibling;
    if (
      beforeWrap &&
      beforeWrap.tagName === "INPUT" &&
      !beforeWrap.classList.contains("emoji-search-input")
    ) {
      return beforeWrap;
    }
  }

  let node = select.previousElementSibling;
  while (node) {
    if (node.tagName === "INPUT" && !node.classList.contains("emoji-search-input")) {
      if (node.name === "emoji" || node.name === "icon") return node;
    }
    node = node.previousElementSibling;
  }

  const label = select.closest("label");
  if (label) {
    const input = label.querySelector('input[name="emoji"], input[name="icon"]');
    if (input && !input.classList.contains("emoji-search-input")) return input;
  }

  const form = select.form;
  if (form) {
    if (select.name === "preset_value") {
      return form.querySelector('input[name="icon"]');
    }
    return form.querySelector('input[name="emoji"], input[name="icon"]');
  }

  return null;
}

function bindIconPresetTarget(select) {
  const target = findIconPresetTarget(select);
  if (target) iconPresetTargets.set(select, target);
  return target;
}

function applyIconPresetSelection(select, value) {
  if (!select || !value) return false;
  const target = bindIconPresetTarget(select);
  if (!target) return false;

  const surface = findRichSurfaceFor(target);
  const icon = parseIconCombined(value);
  if (surface && icon) {
    enhanceRichEmojiField(target);
    insertPremiumImgAtCursor(surface, icon.id, icon.fallback, { replace: true });
    resetPickerUI(select);
    closeEmojiKbdPanel(select);
    surface.focus();
    return true;
  }

  target.value = value;
  target.dispatchEvent(new Event("input", { bubbles: true }));
  if (surface) syncSourceToSurface(target);
  target.focus();
  resetPickerUI(select);
  return true;
}

function resetPickerUI(select) {
  select.selectedIndex = 0;
  const wrap = select.closest(".emoji-search-wrap");
  const search = wrap?.querySelector(".emoji-search-input");
  if (search) search.value = "";
  const results = wrap?.querySelector(".emoji-search-results");
  if (results) {
    results.hidden = true;
    results.innerHTML = "";
  }
  if (typeof select._rebuildOptions === "function") {
    select._rebuildOptions("");
  }
}

function closeEmojiKbdPanel(select) {
  const panel = select?.closest?.(".emoji-kbd-panel");
  if (panel) panel.hidden = true;
}

function bindEmojiKbdToggles(root) {
  (root || document).querySelectorAll("[data-emoji-kbd]").forEach((btn) => {
    if (btn.dataset.bound === "1") return;
    btn.dataset.bound = "1";
    btn.addEventListener("mousedown", (event) => {
      // Save caret before the button steals focus from the contenteditable.
      event.preventDefault();
      const field = btn.closest(".inline-emoji-field");
      const surface = field?.querySelector(".emoji-rich-surface");
      if (surface) saveSurfaceSelection(surface);
    });
    btn.addEventListener("click", (event) => {
      event.preventDefault();
      event.stopPropagation();
      const field = btn.closest(".inline-emoji-field");
      const surface = field?.querySelector(".emoji-rich-surface");
      if (surface) saveSurfaceSelection(surface);
      const panel = field?.querySelector(".emoji-kbd-panel");
      if (!panel) return;
      const willOpen = panel.hidden;
      document.querySelectorAll(".emoji-kbd-panel").forEach((p) => {
        p.hidden = true;
      });
      panel.hidden = !willOpen;
      if (!panel.hidden) {
        enhanceAllEmojiSelects(panel);
        const search = panel.querySelector(".emoji-search-input");
        if (search) {
          search.focus();
          search.dispatchEvent(new Event("focus"));
        }
      }
    });
  });
}

document.addEventListener("click", (event) => {
  const target = event.target;
  if (!(target instanceof Element)) return;
  if (target.closest(".inline-emoji-field")) return;
  document.querySelectorAll(".emoji-kbd-panel").forEach((p) => {
    p.hidden = true;
  });
});

// Stop label activation from yanking focus away from contenteditable surfaces.
document.addEventListener(
  "click",
  (event) => {
    const target = event.target;
    if (!(target instanceof Element)) return;
    if (!target.closest(".emoji-rich-surface, .emoji-kbd-toggle, .emoji-kbd-panel")) return;
    const label = target.closest("label");
    if (!label) return;
    event.preventDefault();
  },
  true
);

function applyPickerSelection(select, item) {
  if (!select || !item || !item.value) return false;
  if (select.classList.contains("emoji-text-picker")) {
    return applyEmojiTextSelection(select, item, item.dataset?.fallback || "✨");
  }
  if (select.classList.contains("icon-preset-select")) {
    return applyIconPresetSelection(select, item.value);
  }
  return false;
}

function emojiIdFromItem(item) {
  if (!item) return "";
  const fromData = (item.dataset && (item.dataset.emojiId || item.dataset.emojiid)) || "";
  if (fromData && /^\d+$/.test(String(fromData))) return String(fromData);
  const value = String(item.value || "");
  if (/^\d+$/.test(value)) return value;
  const match = value.match(/^(\d+)\|/);
  return match ? match[1] : "";
}

function fallbackFromItem(item) {
  if (!item) return "✨";
  if (item.dataset && item.dataset.fallback) return item.dataset.fallback;
  const text = String(item.text || "").trim();
  const first = text.split(/\s+/)[0] || "✨";
  return first;
}

function buildEmojiResultLabel(item) {
  const wrap = document.createElement("span");
  wrap.className = "emoji-result-label";
  const eid = emojiIdFromItem(item);
  const fallback = fallbackFromItem(item);
  const name =
    String(item.text || "")
      .replace(fallback, "")
      .trim() || String(item.text || "").trim();

  if (eid) {
    const img = document.createElement("img");
    img.className = "emoji-premium-thumb";
    img.src = "/admin/custom-emoji/" + eid;
    img.alt = fallback;
    img.loading = "lazy";
    img.width = 22;
    img.height = 22;
    img.addEventListener("error", () => {
      img.replaceWith(document.createTextNode(fallback + " "));
    });
    wrap.appendChild(img);
  } else {
    wrap.appendChild(document.createTextNode(fallback + " "));
  }
  wrap.appendChild(document.createTextNode(name));
  return wrap;
}

/**
 * Add a search box + clickable results above Icon Preset / emoji dropdowns.
 */
function enhanceEmojiSelect(select) {
  if (!select || select.dataset.searchEnhanced === "1") return;

  const isIconPreset = select.classList.contains("icon-preset-select");
  const isTextPicker = select.classList.contains("emoji-text-picker");
  if (!isIconPreset && !isTextPicker) return;

  if (isIconPreset) bindIconPresetTarget(select);

  if (select.options.length < 2) return;

  select.dataset.searchEnhanced = "1";
  const wrap = document.createElement("div");
  wrap.className = "emoji-search-wrap";
  select.parentNode.insertBefore(wrap, select);
  wrap.appendChild(select);

  const search = document.createElement("input");
  search.type = "search";
  search.className = "emoji-search-input";
  search.placeholder = "🔍 Search emoji / icon…";
  search.setAttribute("autocomplete", "off");
  search.setAttribute("aria-label", "Search emoji icons");
  wrap.insertBefore(search, select);

  const results = document.createElement("div");
  results.className = "emoji-search-results";
  results.hidden = true;
  wrap.insertBefore(results, select);

  const allOptions = Array.from(select.options).map((opt, index) => ({
    index,
    value: opt.value,
    text: (opt.textContent || "").trim(),
    disabled: opt.disabled,
    dataset: { ...opt.dataset },
  }));

  function visibleOptions(filter) {
    const q = (filter || "").trim().toLowerCase();
    return allOptions.filter((item) => {
      if (item.index === 0 || !item.value) return !q;
      if (!q) return true;
      return (
        item.text.toLowerCase().includes(q) ||
        String(item.value).toLowerCase().includes(q) ||
        String(emojiIdFromItem(item)).includes(q)
      );
    });
  }

  function rebuild(filter) {
    const matches = visibleOptions(filter);
    select.innerHTML = "";
    matches.forEach((item) => {
      const opt = document.createElement("option");
      opt.value = item.value;
      opt.textContent = item.text;
      opt.disabled = item.disabled;
      Object.keys(item.dataset || {}).forEach((key) => {
        opt.dataset[key] = item.dataset[key];
      });
      select.appendChild(opt);
    });
    // Keep placeholder selected so picking the only filtered match still fires change.
    select.selectedIndex = 0;
  }

  select._rebuildOptions = rebuild;

  function renderResults(filter, { force = false } = {}) {
    const q = (filter || "").trim();
    const matches = visibleOptions(filter).filter((item) => item.value);
    if ((!q && !force) || matches.length === 0) {
      results.hidden = true;
      results.innerHTML = "";
      return;
    }

    results.hidden = false;
    results.innerHTML = "";
    matches.slice(0, force && !q ? 40 : 12).forEach((item) => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "emoji-search-result";
      btn.appendChild(buildEmojiResultLabel(item));
      btn.addEventListener("mousedown", (event) => {
        // Prevent search blur losing the click before handler runs.
        event.preventDefault();
      });
      btn.addEventListener("click", (event) => {
        event.preventDefault();
        applyPickerSelection(select, item);
      });
      results.appendChild(btn);
    });
  }

  search.addEventListener("input", () => {
    rebuild(search.value);
    renderResults(search.value, { force: true });
  });

  search.addEventListener("focus", () => {
    rebuild(search.value);
    renderResults(search.value, { force: true });
  });

  search.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      const matches = visibleOptions(search.value).filter((item) => item.value);
      if (matches.length >= 1) {
        applyPickerSelection(select, matches[0]);
      }
      return;
    }
    if (event.key === "Escape") {
      search.value = "";
      rebuild("");
      renderResults("");
    }
  });

  select.addEventListener("change", () => {
    const option = select.selectedOptions[0];
    if (!option || !option.value) return;
    applyPickerSelection(select, {
      value: option.value,
      text: option.textContent || "",
      dataset: { ...option.dataset },
    });
  });

  select.addEventListener("mousedown", () => {
    // When opening the native list, also show the premium image picker.
    renderResults(search.value, { force: true });
  });
}

function enhanceAllEmojiSelects(root) {
  (root || document)
    .querySelectorAll("select.emoji-text-picker, select.icon-preset-select")
    .forEach(enhanceEmojiSelect);
  (root || document).querySelectorAll("select.premium-named-select").forEach(enhancePremiumNamedSelect);
  bindEmojiKbdToggles(root);
  enhanceAllRichEmojiFields(root);
  initDescriptionTemplates(root);
}

/**
 * Category / product dropdowns: show premium emoji thumbs next to names.
 * Does not rewrite <option> nodes (keeps form values intact).
 */
function enhancePremiumNamedSelect(select) {
  if (!select || select.dataset.premiumNamed === "1") return;
  select.dataset.premiumNamed = "1";

  const wrap = document.createElement("div");
  wrap.className = "premium-named-wrap";
  select.parentNode.insertBefore(wrap, select);
  wrap.appendChild(select);

  const search = document.createElement("input");
  search.type = "search";
  search.className = "emoji-search-input premium-named-search";
  search.placeholder = "🔍 Search…";
  search.setAttribute("autocomplete", "off");
  search.setAttribute("aria-label", "Search options");
  wrap.insertBefore(search, select);

  const results = document.createElement("div");
  results.className = "emoji-search-results premium-named-results";
  results.hidden = true;
  wrap.insertBefore(results, select);

  const display = document.createElement("button");
  display.type = "button";
  display.className = "premium-named-display";
  display.setAttribute("aria-label", "Selected option");
  wrap.insertBefore(display, search);

  function optionMeta(opt) {
    if (!opt) return null;
    return {
      value: opt.value,
      text: (opt.dataset.plainLabel || opt.textContent || "").trim(),
      emojiId: opt.dataset.emojiId || "",
      fallback: opt.dataset.emojiFallback || "📦",
      dataset: { ...opt.dataset },
    };
  }

  function labelFor(meta) {
    const span = document.createElement("span");
    span.className = "emoji-result-label";
    if (meta.emojiId && /^\d+$/.test(meta.emojiId)) {
      const img = document.createElement("img");
      img.className = "emoji-premium-thumb";
      img.src = "/admin/custom-emoji/" + meta.emojiId;
      img.alt = meta.fallback;
      img.loading = "lazy";
      img.width = 22;
      img.height = 22;
      img.addEventListener("error", () => {
        img.replaceWith(document.createTextNode(meta.fallback + " "));
      });
      span.appendChild(img);
    } else {
      span.appendChild(document.createTextNode((meta.fallback || "📦") + " "));
    }
    span.appendChild(document.createTextNode(meta.text || "—"));
    return span;
  }

  function refreshDisplay() {
    display.innerHTML = "";
    const opt = select.options[select.selectedIndex];
    if (!opt || !opt.value) {
      display.appendChild(document.createTextNode((opt && opt.textContent) || "— choose —"));
      return;
    }
    display.appendChild(labelFor(optionMeta(opt)));
  }

  function visibleOptions(filter) {
    const q = (filter || "").trim().toLowerCase();
    return Array.from(select.options)
      .map(optionMeta)
      .filter((item) => {
        if (!item || !item.value) return false;
        if (!q) return true;
        return (
          item.text.toLowerCase().includes(q) ||
          String(item.value).includes(q) ||
          String(item.emojiId).includes(q)
        );
      });
  }

  function renderResults(filter, { force = false } = {}) {
    const q = (filter || "").trim();
    const matches = visibleOptions(filter);
    if ((!q && !force) || matches.length === 0) {
      results.hidden = true;
      results.innerHTML = "";
      return;
    }
    results.hidden = false;
    results.innerHTML = "";
    matches.slice(0, force && !q ? 40 : 16).forEach((item) => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "emoji-search-result";
      if (item.value === select.value) btn.classList.add("is-selected");
      btn.appendChild(labelFor(item));
      btn.addEventListener("mousedown", (event) => event.preventDefault());
      btn.addEventListener("click", (event) => {
        event.preventDefault();
        select.value = item.value;
        select.dispatchEvent(new Event("change", { bubbles: true }));
        refreshDisplay();
        search.value = "";
        results.hidden = true;
        results.innerHTML = "";
      });
      results.appendChild(btn);
    });
  }

  display.addEventListener("click", () => {
    search.focus();
    renderResults(search.value, { force: true });
  });

  search.addEventListener("input", () => renderResults(search.value, { force: true }));
  search.addEventListener("focus", () => renderResults(search.value, { force: true }));
  search.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      search.value = "";
      results.hidden = true;
      results.innerHTML = "";
    }
    if (event.key === "Enter") {
      event.preventDefault();
      const matches = visibleOptions(search.value);
      if (matches.length >= 1) {
        select.value = matches[0].value;
        select.dispatchEvent(new Event("change", { bubbles: true }));
        refreshDisplay();
        search.value = "";
        results.hidden = true;
        results.innerHTML = "";
      }
    }
  });

  select.addEventListener("change", refreshDisplay);
  select.classList.add("premium-named-native");
  refreshDisplay();
}

function initDescriptionTemplates(root) {
  const scope = root || document;

  scope.querySelectorAll(".desc-template-select").forEach((select) => {
    if (select.dataset.tplBound === "1") return;
    select.dataset.tplBound = "1";
    const targetId = select.dataset.descTarget;
    const jsonEl = document.querySelector(`.desc-templates-json[data-desc-target="${targetId}"]`);
    let templates = [];
    try {
      templates = JSON.parse((jsonEl && jsonEl.textContent) || "[]");
    } catch (_) {
      templates = [];
    }
    const byId = {};
    templates.forEach((tpl) => {
      byId[String(tpl.id)] = tpl;
    });
    select._tplById = byId;

    select.addEventListener("change", () => {
      const tpl = select._tplById[String(select.value)];
      if (!tpl) return;
      const field = document.getElementById(targetId);
      if (!field) return;
      field.value = tpl.body || "";
      syncSourceToSurface(field);
    });
  });

  scope.querySelectorAll(".desc-template-save-btn").forEach((btn) => {
    if (btn.dataset.tplBound === "1") return;
    btn.dataset.tplBound = "1";
    btn.addEventListener("click", async () => {
      const targetId = btn.dataset.descTarget;
      const nameInput = document.querySelector(`.desc-template-name[data-desc-target="${targetId}"]`);
      const field = document.getElementById(targetId);
      const surface = findRichSurfaceFor(field);
      if (surface) syncSurfaceToSource(surface);
      const name = ((nameInput && nameInput.value) || "").trim();
      const body = (field && field.value) || "";
      if (!name) {
        window.alert("Template name is required");
        return;
      }
      if (!body.trim()) {
        window.alert("Description is empty — write the box content first");
        return;
      }
      btn.disabled = true;
      try {
        const form = new FormData();
        form.append("name", name);
        form.append("body", body);
        const res = await fetch("/admin/description-templates", {
          method: "POST",
          body: form,
          credentials: "same-origin",
          headers: { "X-Requested-With": "fetch", Accept: "application/json" },
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok || !data.ok) {
          window.alert((data && data.error) || "Could not save template");
          return;
        }
        const select = document.querySelector(`.desc-template-select[data-desc-target="${targetId}"]`);
        if (select) {
          if (!select._tplById) select._tplById = {};
          select._tplById[String(data.id)] = { id: data.id, name: data.name, body: data.body };
          let opt = Array.from(select.options).find((o) => o.value === String(data.id));
          if (!opt) {
            opt = document.createElement("option");
            opt.value = String(data.id);
            select.appendChild(opt);
          }
          opt.textContent = data.name;
          select.value = String(data.id);
          const jsonEl = document.querySelector(`.desc-templates-json[data-desc-target="${targetId}"]`);
          if (jsonEl) {
            jsonEl.textContent = JSON.stringify(Object.values(select._tplById));
          }
        }
        if (nameInput) nameInput.value = "";
        const details = btn.closest("details");
        if (details) details.open = false;
        window.alert("Template saved");
      } catch (_) {
        window.alert("Could not save template");
      } finally {
        btn.disabled = false;
      }
    });
  });
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", () => enhanceAllEmojiSelects());
} else {
  enhanceAllEmojiSelects();
}

document.addEventListener(
  "toggle",
  (event) => {
    if (event.target instanceof HTMLDetailsElement && event.target.open) {
      enhanceAllEmojiSelects(event.target);
    }
  },
  true
);
