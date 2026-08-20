const tg = window.Telegram && window.Telegram.WebApp;
if (tg) {
  tg.ready();
  tg.expand();
  try { tg.setHeaderColor("#09051A"); } catch (err) { /* older clients */ }
  try { tg.setBackgroundColor("#09051A"); } catch (err) { /* older clients */ }
}

const ACCENTS = ["violet", "teal", "green", "rose"];
const CURRENCIES = [
  { code: "USD", label: "USD ($)" },
  { code: "PKR", label: "PKR (Rs.)" },
  { code: "EUR", label: "EUR (€)" },
  { code: "GBP", label: "GBP (£)" },
  { code: "INR", label: "INR (₹)" },
];
const LANGUAGES = [
  { code: "en", label: "English", flag: "🇬🇧" },
  { code: "es", label: "Español", flag: "🇪🇸" },
  { code: "ar", label: "العربية", flag: "🇸🇦" },
  { code: "hi", label: "हिंदी", flag: "🇮🇳" },
  { code: "ru", label: "Русский", flag: "🇷🇺" },
  { code: "vi", label: "Tiếng Việt", flag: "🇻🇳" },
  { code: "zh", label: "中文", flag: "🇨🇳" },
];

const state = {
  products: [],
  categories: [],
  categoryId: null,
  query: "",
  priceRange: "all",
  platform: "all",
  inStockOnly: false,
  notes: {},
  cart: JSON.parse(localStorage.getItem("smf-mini-cart") || "[]"),
  currency: CURRENCIES[0],
  language: LANGUAGES[0],
};

const els = {
  grid: document.getElementById("grid"),
  cats: document.getElementById("cats"),
  empty: document.getElementById("empty"),
  search: document.getElementById("search"),
  headerSearch: document.getElementById("header-search"),
  cartCount: document.getElementById("cart-count"),
  cartBtn: document.getElementById("cart-btn"),
  cartOverlay: document.getElementById("cart-overlay"),
  cartPanel: document.getElementById("cart-panel"),
  detailOverlay: document.getElementById("detail-overlay"),
  detailPanel: document.getElementById("detail-panel"),
  source: document.getElementById("catalog-source"),
  header: document.getElementById("site-header"),
  mobileMenu: document.getElementById("mobile-menu"),
};

function escapeHtml(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function money(value) {
  return `$${Number(value || 0).toFixed(2)}`;
}

function discountPercent(product) {
  if (!product.original_price || product.original_price <= product.sell_price) return null;
  return Math.round((1 - product.sell_price / product.original_price) * 100);
}

function inPriceRange(price, range) {
  if (range === "under5") return price < 5;
  if (range === "5to15") return price >= 5 && price <= 15;
  if (range === "over15") return price > 15;
  return true;
}

function saveCart() {
  localStorage.setItem("smf-mini-cart", JSON.stringify(state.cart));
  const count = state.cart.reduce((sum, item) => sum + item.qty, 0);
  els.cartCount.textContent = String(count);
  els.cartCount.classList.toggle("hidden", count === 0);
}

function addToCart(product) {
  if (!product.in_stock) return;
  const existing = state.cart.find((item) => item.sku === product.sku);
  if (existing) existing.qty += 1;
  else state.cart.push({ sku: product.sku, name: product.name, price: product.sell_price, qty: 1 });
  saveCart();
}

function setQty(sku, delta) {
  const item = state.cart.find((row) => row.sku === sku);
  if (!item) return;
  item.qty += delta;
  if (item.qty <= 0) state.cart = state.cart.filter((row) => row.sku !== sku);
  saveCart();
  renderCart();
}

function filtered() {
  const q = state.query.trim().toLowerCase();
  return state.products.filter((product) => {
    if (state.categoryId != null && product.category_id !== state.categoryId) return false;
    if (state.inStockOnly && !product.in_stock) return false;
    if (!inPriceRange(Number(product.sell_price || 0), state.priceRange)) return false;
    if (state.platform !== "all" && product.platform && product.platform !== state.platform) return false;
    if (q) {
      const hay = `${product.name || ""} ${product.sku || ""} ${product.description || ""} ${product.category || ""}`.toLowerCase();
      if (!hay.includes(q)) return false;
    }
    return true;
  });
}

function countFor(id) {
  if (id == null) return state.products.length;
  return state.products.filter((product) => product.category_id === id).length;
}

function renderCats() {
  const all = [{ id: null, name: "All", emoji: "✨" }, ...state.categories];
  els.cats.innerHTML = all
    .map((cat) => {
      const active = cat.id === state.categoryId ? " active" : "";
      return `<button type="button" class="category-pill${active}" data-id="${cat.id ?? ""}" role="listitem">
        <span aria-hidden>${escapeHtml(cat.emoji || "")}</span>
        <span>${escapeHtml(cat.name)}</span>
        <span class="category-pill-count">${countFor(cat.id)}</span>
      </button>`;
    })
    .join("");
  els.cats.querySelectorAll(".category-pill").forEach((btn) => {
    btn.addEventListener("click", () => {
      const raw = btn.getAttribute("data-id");
      state.categoryId = raw ? Number(raw) : null;
      renderCats();
      renderGrid();
    });
  });
}

function stockClass(label) {
  if (label === "Out of stock") return "stock-out";
  if (label === "Low stock") return "stock-low";
  return "stock-ok";
}

function productCard(product, index) {
  const discount = discountPercent(product);
  const accent = ACCENTS[index % ACCENTS.length];
  const warranty = product.warranty_percent ?? 70;
  const noteOpen = Boolean(state.notes[product.sku]);
  const image = product.image_url
    ? `<button type="button" class="product-card-image" data-detail="${escapeHtml(product.sku)}">
        <img class="product-card-image-el" src="${escapeHtml(product.image_url)}" alt="${escapeHtml(product.name)}">
      </button>`
    : "";
  const sale = discount
    ? `<div class="card-badges"><span class="pill-badge sale">Sale −${discount}%</span></div>`
    : "";
  const emoji = product.image_url
    ? ""
    : `<div class="product-emoji" aria-hidden>${escapeHtml(product.emoji || "🛍️")}</div>`;
  const warrantyBlock = product.warranty_label
    ? `<div class="warranty-row"><span aria-hidden>🛡️</span> ${escapeHtml(product.warranty_label)}</div>
       <div class="warranty-bar"><div class="warranty-bar-fill" style="width:${warranty}%"></div></div>`
    : "";
  const noteBtn = product.note
    ? `<button type="button" class="view-note-btn" data-note="${escapeHtml(product.sku)}">
        <span aria-hidden>📋</span> ${noteOpen ? "Hide note" : "View note"}
      </button>`
    : "";
  const noteBody = noteOpen && product.note
    ? `<p class="card-desc">${escapeHtml(product.note)}</p>`
    : "";

  return `<article class="product-card accent-${accent}">
    ${sale}
    ${image}
    <div class="card-top-row">
      <span class="card-info-btn" title="${escapeHtml(product.description || product.name)}" aria-label="About ${escapeHtml(product.name)}">i</span>
      ${product.delivery_type === "manual" ? "" : '<span class="instant-badge"><span aria-hidden>⚡</span> Instant</span>'}
    </div>
    <div class="product-card-top">
      ${emoji}
      <div>
        <h3><a href="#product-${encodeURIComponent(product.sku)}" data-detail="${escapeHtml(product.sku)}">${escapeHtml(product.name)}</a></h3>
        <div class="product-meta">${escapeHtml(product.category || "General")} · <span class="${stockClass(product.stock_label)}">${escapeHtml(product.stock_label)}</span></div>
      </div>
    </div>
    ${product.description ? `<p class="card-desc">${escapeHtml(product.description)}</p>` : ""}
    ${warrantyBlock}
    ${noteBtn}
    ${noteBody}
    <div class="card-meta-row">
      <div class="price-row" style="flex-direction:column;gap:0;align-items:flex-start">
        <span class="price-only">Only</span>
        <span class="price-value">${money(product.sell_price)}</span>
        ${discount ? `<div class="price-original">${money(product.original_price)}</div>` : ""}
      </div>
      ${product.stock != null ? `<span class="stock-count"><span class="stock-dot${product.in_stock ? " ok" : ""}"></span>${escapeHtml(product.stock)}</span>` : ""}
    </div>
    <div class="card-actions">
      <button type="button" class="btn btn-ghost" data-detail="${escapeHtml(product.sku)}">Details</button>
      <button type="button" class="btn btn-accent" data-add="${escapeHtml(product.sku)}" ${product.in_stock ? "" : "disabled"}>
        <span aria-hidden>🛒</span> Add to Cart
      </button>
    </div>
  </article>`;
}

function renderGrid() {
  const rows = filtered();
  els.empty.classList.toggle("hidden", rows.length > 0);
  if (!rows.length && state.products.length) {
    els.grid.innerHTML = "";
    return;
  }
  els.grid.innerHTML = rows.map((product, index) => productCard(product, index)).join("");
  els.grid.querySelectorAll("[data-add]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const product = state.products.find((row) => row.sku === btn.getAttribute("data-add"));
      if (product) addToCart(product);
    });
  });
  els.grid.querySelectorAll("[data-detail]").forEach((btn) => {
    btn.addEventListener("click", (event) => {
      event.preventDefault();
      openDetail(btn.getAttribute("data-detail"));
    });
  });
  els.grid.querySelectorAll("[data-note]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const sku = btn.getAttribute("data-note");
      state.notes[sku] = !state.notes[sku];
      renderGrid();
    });
  });
}

function renderSkeleton() {
  els.grid.innerHTML = Array.from({ length: 3 })
    .map(
      () => `<article class="product-card skeleton-card">
        <div class="skeleton-block skeleton-image"></div>
        <div class="product-card-top">
          <div class="skeleton-block skeleton-emoji"></div>
          <div style="flex:1">
            <div class="skeleton-block skeleton-line" style="width:70%"></div>
            <div class="skeleton-block skeleton-line" style="width:40%"></div>
          </div>
        </div>
        <div class="card-actions">
          <div class="skeleton-block skeleton-btn"></div>
          <div class="skeleton-block skeleton-btn"></div>
        </div>
      </article>`,
    )
    .join("");
}

function openDetail(sku) {
  const product = state.products.find((row) => row.sku === sku);
  if (!product) return;
  const discount = discountPercent(product);
  els.detailPanel.innerHTML = `
    <button type="button" class="icon-btn overlay-close" id="detail-close" aria-label="Close">✕</button>
    <h2>${escapeHtml(product.emoji || "")} ${escapeHtml(product.name)}</h2>
    <p class="product-meta">${escapeHtml(product.category || "General")} · ${escapeHtml(product.stock_label)}</p>
    <p class="price-value">${money(product.sell_price)}${discount ? ` <span class="price-original">${money(product.original_price)}</span>` : ""}</p>
    <p class="muted">${escapeHtml(product.description || "")}</p>
    ${product.warranty_label ? `<p class="warranty-row"><span aria-hidden>🛡️</span> ${escapeHtml(product.warranty_label)}</p>` : ""}
    ${product.note ? `<p class="card-desc">${escapeHtml(product.note)}</p>` : ""}
    <div class="card-actions">
      <button type="button" class="btn btn-ghost" id="detail-close-2">Close</button>
      <button type="button" class="btn btn-accent" id="detail-add" ${product.in_stock ? "" : "disabled"}>
        <span aria-hidden>🛒</span> Add to Cart
      </button>
    </div>
  `;
  els.detailOverlay.classList.remove("hidden");
  const close = () => els.detailOverlay.classList.add("hidden");
  document.getElementById("detail-close").onclick = close;
  document.getElementById("detail-close-2").onclick = close;
  document.getElementById("detail-add").onclick = () => {
    addToCart(product);
    close();
  };
}

function renderCart() {
  if (!state.cart.length) {
    els.cartPanel.innerHTML = `
      <button type="button" class="icon-btn overlay-close" id="cart-close" aria-label="Close">✕</button>
      <h2>Your cart</h2>
      <p class="muted">Cart is empty. Add a product, then complete the order in SMF SHOP with Shop All.</p>
      <button type="button" class="btn btn-primary" id="cart-close-2">Continue shopping</button>
    `;
  } else {
    const total = state.cart.reduce((sum, item) => sum + item.price * item.qty, 0);
    const lines = state.cart
      .map(
        (item) => `<div class="cart-line">
          <div>
            <strong>${escapeHtml(item.name)}</strong>
            <div class="muted">${money(item.price)} each</div>
            <div class="qty-row">
              <button type="button" data-qty="${escapeHtml(item.sku)}" data-delta="-1">−</button>
              <span>${item.qty}</span>
              <button type="button" data-qty="${escapeHtml(item.sku)}" data-delta="1">+</button>
            </div>
          </div>
          <strong>${money(item.price * item.qty)}</strong>
        </div>`,
      )
      .join("");
    els.cartPanel.innerHTML = `
      <button type="button" class="icon-btn overlay-close" id="cart-close" aria-label="Close">✕</button>
      <h2>Your cart</h2>
      ${lines}
      <p class="price">Total ${money(total)}</p>
      <p class="muted">Checkout SMF SHOP bot ke Shop All se complete hota hai — yahan live catalog hai.</p>
      <div class="card-actions">
        <button type="button" class="btn btn-ghost" id="cart-clear">Clear</button>
        <button type="button" class="btn btn-primary" id="cart-close-2">Close</button>
      </div>
    `;
    els.cartPanel.querySelectorAll("[data-qty]").forEach((btn) => {
      btn.addEventListener("click", () => setQty(btn.getAttribute("data-qty"), Number(btn.getAttribute("data-delta"))));
    });
    document.getElementById("cart-clear").onclick = () => {
      state.cart = [];
      saveCart();
      renderCart();
    };
  }
  const close = () => els.cartOverlay.classList.add("hidden");
  document.getElementById("cart-close").onclick = close;
  document.getElementById("cart-close-2").onclick = close;
  els.cartOverlay.classList.remove("hidden");
}

function setQuery(value) {
  state.query = value;
  els.search.value = value;
  if (els.headerSearch) els.headerSearch.value = value;
  renderGrid();
}

function tickCountdown() {
  const now = new Date();
  const midnight = new Date(now);
  midnight.setHours(24, 0, 0, 0);
  const total = Math.max(0, Math.floor((midnight.getTime() - now.getTime()) / 1000));
  const h = String(Math.floor(total / 3600)).padStart(2, "0");
  const m = String(Math.floor((total % 3600) / 60)).padStart(2, "0");
  const s = String(total % 60).padStart(2, "0");
  document.getElementById("cd-h").textContent = h;
  document.getElementById("cd-m").textContent = m;
  document.getElementById("cd-s").textContent = s;
}

function bindMenus() {
  const currencyPanel = document.getElementById("currency-panel");
  const languagePanel = document.getElementById("language-panel");
  currencyPanel.innerHTML = CURRENCIES.map(
    (c) => `<button type="button" class="dropdown-item${c.code === state.currency.code ? " active" : ""}" data-currency="${c.code}">${escapeHtml(c.label)}</button>`,
  ).join("");
  languagePanel.innerHTML = LANGUAGES.map(
    (l) => `<button type="button" class="dropdown-item${l.code === state.language.code ? " active" : ""}" data-language="${l.code}"><span aria-hidden>${l.flag}</span> ${escapeHtml(l.label)}</button>`,
  ).join("");

  document.getElementById("currency-btn").onclick = (event) => {
    event.stopPropagation();
    currencyPanel.classList.toggle("hidden");
    languagePanel.classList.add("hidden");
  };
  document.getElementById("language-btn").onclick = (event) => {
    event.stopPropagation();
    languagePanel.classList.toggle("hidden");
    currencyPanel.classList.add("hidden");
  };
  currencyPanel.onclick = (event) => event.stopPropagation();
  languagePanel.onclick = (event) => event.stopPropagation();
  currencyPanel.querySelectorAll("[data-currency]").forEach((btn) => {
    btn.onclick = () => {
      state.currency = CURRENCIES.find((c) => c.code === btn.getAttribute("data-currency"));
      document.getElementById("currency-label").textContent = state.currency.label;
      currencyPanel.classList.add("hidden");
      bindMenus();
    };
  });
  languagePanel.querySelectorAll("[data-language]").forEach((btn) => {
    btn.onclick = () => {
      state.language = LANGUAGES.find((l) => l.code === btn.getAttribute("data-language"));
      document.getElementById("language-label").textContent = state.language.label;
      document.getElementById("language-flag").textContent = state.language.flag;
      languagePanel.classList.add("hidden");
      bindMenus();
    };
  });
}

function setMenuOpen(open) {
  els.mobileMenu.classList.toggle("open", open);
  els.mobileMenu.setAttribute("aria-hidden", open ? "false" : "true");
}

async function load() {
  renderSkeleton();
  const [catRes, prodRes] = await Promise.all([
    fetch("/api/web/categories", { cache: "no-store" }),
    fetch("/api/web/products", { cache: "no-store" }),
  ]);
  if (!catRes.ok || !prodRes.ok) {
    els.source.textContent = "Catalog is updating. Open Shop All in the bot if this stays empty.";
    els.empty.textContent = "Could not load live products.";
    els.empty.classList.remove("hidden");
    els.grid.innerHTML = "";
    return;
  }
  state.categories = await catRes.json();
  state.products = await prodRes.json();
  els.source.textContent = "Live products from SMF Shop";
  renderCats();
  renderGrid();
}

document.getElementById("year").textContent = String(new Date().getFullYear());
saveCart();
bindMenus();
document.addEventListener("click", () => {
  document.getElementById("currency-panel").classList.add("hidden");
  document.getElementById("language-panel").classList.add("hidden");
});
tickCountdown();
setInterval(tickCountdown, 1000);

window.addEventListener("scroll", () => {
  els.header.classList.toggle("scrolled", window.scrollY > 8);
}, { passive: true });

els.search.addEventListener("input", () => setQuery(els.search.value));
els.headerSearch.addEventListener("input", () => setQuery(els.headerSearch.value));
document.getElementById("header-search-form").addEventListener("submit", (event) => {
  event.preventDefault();
  setQuery(els.headerSearch.value);
  document.getElementById("catalog").scrollIntoView({ behavior: "smooth" });
});

document.getElementById("pricing-filter").addEventListener("change", (event) => {
  state.priceRange = event.target.value;
  renderGrid();
});
document.getElementById("platform-filter").addEventListener("change", (event) => {
  state.platform = event.target.value;
  renderGrid();
});
document.getElementById("stock-only").addEventListener("click", (event) => {
  state.inStockOnly = !state.inStockOnly;
  event.currentTarget.classList.toggle("active", state.inStockOnly);
  event.currentTarget.setAttribute("aria-pressed", state.inStockOnly ? "true" : "false");
  renderGrid();
});

document.getElementById("cats-left").onclick = () => els.cats.scrollBy({ left: -220, behavior: "smooth" });
document.getElementById("cats-right").onclick = () => els.cats.scrollBy({ left: 220, behavior: "smooth" });

els.cartBtn.addEventListener("click", renderCart);
document.getElementById("footer-cart").addEventListener("click", (event) => {
  event.preventDefault();
  renderCart();
});
document.getElementById("mobile-cart").addEventListener("click", () => {
  setMenuOpen(false);
  renderCart();
});
els.cartOverlay.addEventListener("click", (event) => {
  if (event.target === els.cartOverlay) els.cartOverlay.classList.add("hidden");
});
els.detailOverlay.addEventListener("click", (event) => {
  if (event.target === els.detailOverlay) els.detailOverlay.classList.add("hidden");
});

document.getElementById("menu-toggle").onclick = () => setMenuOpen(true);
document.getElementById("menu-close").onclick = () => setMenuOpen(false);
els.mobileMenu.addEventListener("click", (event) => {
  if (event.target === els.mobileMenu) setMenuOpen(false);
});
els.mobileMenu.querySelectorAll("[data-close-menu]").forEach((link) => {
  link.addEventListener("click", () => setMenuOpen(false));
});

document.getElementById("theme-toggle").addEventListener("click", () => {
  const light = document.documentElement.classList.toggle("theme-light");
  document.getElementById("theme-toggle").textContent = light ? "☀️" : "🌙";
});

const chatBtn = document.getElementById("chat-btn");
const chatPanel = document.getElementById("chat-panel");
function toggleChat() {
  const open = chatPanel.classList.toggle("hidden") === false;
  chatBtn.textContent = open ? "✕" : "💬";
  chatBtn.setAttribute("aria-label", open ? "Close assistant" : "Open assistant");
}
chatBtn.onclick = toggleChat;
document.getElementById("chat-close").onclick = () => {
  chatPanel.classList.add("hidden");
  chatBtn.textContent = "💬";
};

load().catch(() => {
  els.empty.textContent = "Could not load live products.";
  els.empty.classList.remove("hidden");
  els.grid.innerHTML = "";
});
