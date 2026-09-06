(() => {
  const tg = window.Telegram && window.Telegram.WebApp;
  if (tg) {
    tg.ready();
    tg.expand();
    try {
      tg.setHeaderColor("#09070f");
      tg.setBackgroundColor("#09070f");
    } catch (_err) {
      /* older clients */
    }
  }

  const ACCENTS = ["violet", "teal", "green", "rose"];
  const FLAG_ISO = {
    USD: "us",
    PKR: "pk",
    EUR: "eu",
    GBP: "gb",
    INR: "in",
    en: "gb",
    es: "es",
    ar: "sa",
    hi: "in",
    ru: "ru",
    vi: "vn",
    zh: "cn",
    fa: "ir",
    id: "id",
    ko: "kr",
    ur: "pk",
  };

  let cachedProds = [];
  let cachedCats = [];
  let cachedFeat = { live: [], hot: [], best_seller: [] };
  let cachedShop = null;
  try {
    cachedProds = JSON.parse(localStorage.getItem("smf_cache_products") || "[]");
    cachedCats = JSON.parse(localStorage.getItem("smf_cache_categories") || "[]");
    cachedFeat = JSON.parse(localStorage.getItem("smf_cache_featured") || '{"live":[],"hot":[],"best_seller":[]}');
    cachedShop = JSON.parse(localStorage.getItem("smf_cache_shop") || "null");
  } catch (_e) {}

  const state = {
    shop: cachedShop,
    products: cachedProds,
    featured: cachedFeat,
    categories: cachedCats,
    methods: [],
    currency: localStorage.getItem("smf_currency") || "USD",
    language: localStorage.getItem("smf_language") || "en",
    query: "",
    categoryId: null,
    notes: {},
    cart: JSON.parse(localStorage.getItem("smf_cart") || "[]"),
    user: JSON.parse(localStorage.getItem("smf_user") || "null"),
    route: "/",
    order: null,
  };

  const els = {
    eyebrow: document.getElementById("shop-eyebrow"),
    headline: document.getElementById("shop-headline"),
    tagline: document.getElementById("shop-tagline"),
    whatsapp: document.getElementById("btn-whatsapp"),
    whatsappCatalog: document.getElementById("btn-whatsapp-catalog"),
    whatsappCheckout: document.getElementById("btn-whatsapp-checkout"),
    search: document.getElementById("search-input"),
    currencyBtn: document.getElementById("currency-btn"),
    currencyMenu: document.querySelector("#currency-dd .menu"),
    languageBtn: document.getElementById("language-btn"),
    languageMenu: document.querySelector("#language-dd .menu"),
    cartCount: document.getElementById("cart-count"),
    live: document.getElementById("rail-live"),
    hot: document.getElementById("rail-hot"),
    best: document.getElementById("rail-best"),
    liveCount: document.getElementById("live-count"),
    hotCount: document.getElementById("hot-count"),
    bestCount: document.getElementById("best-count"),
    pills: document.getElementById("category-pills"),
    grid: document.getElementById("product-grid"),
    collectionGrid: document.getElementById("collection-grid"),
    productSheet: document.getElementById("product-sheet"),
    productBody: document.getElementById("product-sheet-body"),
    cartSheet: document.getElementById("cart-overlay"),
    cartBody: document.getElementById("cart-body"),
    viewHome: document.getElementById("view-home"),
    viewCollection: document.getElementById("view-collection"),
    viewAuth: document.getElementById("view-auth"),
    viewCheckout: document.getElementById("view-checkout"),
    viewOrder: document.getElementById("view-order"),
  };

  function currentPath() {
    const raw = (location.hash || "#/").replace(/^#/, "") || "/";
    return raw.startsWith("/") ? raw : `/${raw}`;
  }

  async function getJSON(path) {
    const res = await fetch(path, { headers: { Accept: "application/json" } });
    if (!res.ok) throw new Error(`${path} failed`);
    return res.json();
  }

  async function postJSON(path, body) {
    const res = await fetch(path, {
      method: "POST",
      headers: { Accept: "application/json", "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      const detail = data.detail;
      const msg = Array.isArray(detail)
        ? detail.map((row) => row.msg || row).join(" ")
        : detail || `${path} failed`;
      throw new Error(typeof msg === "string" ? msg : JSON.stringify(msg));
    }
    return data;
  }

  function formatPrice(amount) {
    const value = Number(amount || 0);
    if (state.currency === "PKR") {
      const rate = Number(state.shop && state.shop.pkr_rate) || 280;
      const pkr = value * rate;
      return `Rs. ${pkr.toLocaleString("en-PK", { maximumFractionDigits: 0 })}`;
    }
    return `$${value.toFixed(2)}`;
  }

  function usdPrice(amount) {
    return `$${Number(amount || 0).toFixed(2)}`;
  }

  function pkrPrice(amount) {
    const rate = Number(state.shop && state.shop.pkr_rate) || 280;
    const pkr = Number(amount || 0) * rate;
    return `≈ Rs. ${pkr.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })} PKR`;
  }

  function discountPercent(product) {
    if (!product.original_price || product.original_price <= product.sell_price) return null;
    return Math.round((1 - product.sell_price / product.original_price) * 100);
  }

  function saveCart() {
    localStorage.setItem("smf_cart", JSON.stringify(state.cart));
    renderCartCount();
  }

  function saveUser(user) {
    state.user = user;
    if (user) localStorage.setItem("smf_user", JSON.stringify(user));
    else localStorage.removeItem("smf_user");
    renderChrome();
  }

  function addToCart(product) {
    const existing = state.cart.find((row) => row.sku === product.sku);
    if (existing) existing.qty += 1;
    else {
      state.cart.push({
        sku: product.sku,
        name: product.name,
        emoji: product.emoji,
        sell_price: product.sell_price,
        qty: 1,
      });
    }
    saveCart();
  }

  function removeFromCart(sku) {
    state.cart = state.cart.filter((item) => item.sku !== sku);
    saveCart();
  }

  function cartTotal() {
    return state.cart.reduce((sum, row) => sum + row.sell_price * row.qty, 0);
  }

  function waHref(product) {
    const base = (state.shop && state.shop.whatsapp_url) || "";
    if (!base) return state.shop && state.shop.support_url ? state.shop.support_url : "#";
    let text = "Hi SMF SHOP, I want to place an order from the Mini App.";
    if (product) {
      text = `Hi SMF SHOP, I want to order ${product.name} (${product.sku}) for ${formatPrice(product.sell_price)}`;
    } else if (state.cart.length) {
      const lines = state.cart.map((row) => `${row.qty}x ${row.name} (${formatPrice(row.sell_price)})`).join(", ");
      text = `Hi SMF SHOP, I want to order: ${lines}. Total ${formatPrice(cartTotal())}`;
    }
    return `${base}?text=${encodeURIComponent(text)}`;
  }

  function closeMenus() {
    document.querySelectorAll(".menu").forEach((menu) => {
      menu.hidden = true;
    });
    document.querySelectorAll(".chip").forEach((btn) => btn.setAttribute("aria-expanded", "false"));
  }

  function flagIso(item) {
    return (item && (item.flag_iso || FLAG_ISO[item.code])) || "xx";
  }

  function flagMarkup(item) {
    const iso = flagIso(item);
    const label = escapeHtml(item.label || item.name || item.code || "");
    return `<img class="flag-img" src="/static/mini-app/flags/${iso}.svg" alt="" width="20" height="14" onerror="this.onerror=null;this.src='/static/mini-app/flags/xx.svg'"> ${label}`;
  }

  function renderMenus() {
    const currencies = (state.shop && state.shop.currencies) || [
      { code: "USD", label: "USD ($)", flag_iso: "us" },
      { code: "PKR", label: "PKR (Rs.)", flag_iso: "pk" },
    ];
    els.currencyMenu.innerHTML = currencies
      .map((item) => `<button type="button" data-currency="${item.code}">${flagMarkup(item)}</button>`)
      .join("");
    const current = currencies.find((item) => item.code === state.currency) || currencies[0];
    els.currencyBtn.innerHTML = flagMarkup(current);

    const languages = (state.shop && state.shop.languages) || [{ code: "en", name: "English", flag_iso: "gb" }];
    els.languageMenu.innerHTML = languages
      .map((item) => `<button type="button" data-language="${item.code}">${flagMarkup(item)}</button>`)
      .join("");
    const lang = languages.find((item) => item.code === state.language) || languages[0];
    state.language = lang.code;
    els.languageBtn.innerHTML = flagMarkup(lang);
  }

  function renderChrome() {
    if (state.shop) {
      if (els.eyebrow && state.shop.eyebrow) els.eyebrow.textContent = state.shop.eyebrow;
      if (els.headline && state.shop.headline) els.headline.textContent = state.shop.headline;
      if (els.tagline && state.shop.tagline) els.tagline.textContent = state.shop.tagline;
      [els.whatsapp, els.whatsappCatalog, els.whatsappCheckout].forEach((node) => {
        if (!node) return;
        node.href = waHref();
        node.hidden = false;
        node.style.display = "";
      });
    }
    const signed = Boolean(state.user && state.user.email);
    const accountBtn = document.getElementById("btn-account");
    if (accountBtn) accountBtn.href = signed ? "/account" : "#/signup";
    const accountLabel = document.getElementById("account-pill-label");
    if (accountLabel) accountLabel.textContent = signed ? (state.user.name || "VIP") : "VIP";
    const balanceDisplay = document.getElementById("topbar-balance-display");
    if (balanceDisplay) {
      const bal = state.user && state.user.wallet_balance != null ? `$${Number(state.user.wallet_balance).toFixed(2)}` : "$148.50";
      balanceDisplay.textContent = bal;
    }

    document.querySelectorAll(".nav-link").forEach((link) => {
      const href = link.getAttribute("href") || "";
      const isHome = link.id === "nav-home" || href === "/mini" || href === "#/";
      link.classList.toggle(
        "active",
        (isHome && state.route === "/") || href === `#${state.route}`
      );
    });
    renderMenus();
    renderCartCount();
  }

  function renderCartCount() {
    const count = state.cart.reduce((sum, row) => sum + row.qty, 0);
    els.cartCount.hidden = count === 0;
    els.cartCount.textContent = String(count);

    // Badge bounce animation
    if (count > 0 && document.getElementById("btn-cart")) {
      const cartBtn = document.getElementById("btn-cart");
      cartBtn.classList.remove("badge-bounce-anim");
      void cartBtn.offsetWidth; // trigger reflow
      cartBtn.classList.add("badge-bounce-anim");
      setTimeout(() => cartBtn.classList.remove("badge-bounce-anim"), 300);
    }
  }

  function showToast(message) {
    const container = document.getElementById("toast-container");
    if (!container) return;
    const toast = document.createElement("div");
    toast.className = "toast";
    toast.textContent = message;
    container.appendChild(toast);

    setTimeout(() => {
      toast.classList.add("toast-out");
      setTimeout(() => toast.remove(), 250);
    }, 2500);
  }

  function escapeHtml(value) {
    return String(value || "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function itemButton(product, tag) {
    const icon = product.image_url ? `<img src="${product.image_url}" alt="">` : product.emoji || "🛍️";
    return `
      <button type="button" class="item" data-sku="${product.sku}">
        <span class="item-icon">${icon}</span>
        <span>
          <span class="item-name">${escapeHtml(product.name)}</span>
          <span class="item-meta">${escapeHtml(product.category || "General")}</span>
        </span>
        <span style="text-align:right">
          <span class="item-price">${formatPrice(product.sell_price)}</span>
          ${tag ? `<div><span class="tag ${tag}">${tag}</span></div>` : ""}
        </span>
      </button>
    `;
  }

  function renderRail(el, countEl, rows, tag, emptyText) {
    countEl.textContent = String(rows.length);
    if (!rows.length) {
      el.innerHTML = `<p class="empty">${emptyText}</p>`;
      return;
    }
    el.innerHTML = rows.map((row) => itemButton(row, tag)).join("");
  }

  function renderFeatured() {
    renderRail(els.live, els.liveCount, state.featured.live || [], "live", "No live stock yet.");
    renderRail(els.hot, els.hotCount, state.featured.hot || [], "hot", "No hot deals right now.");
    renderRail(els.best, els.bestCount, state.featured.best_seller || [], "best", "Best sellers will appear here.");
  }

  function filteredProducts(kind) {
    const q = state.query.trim().toLowerCase();
    return state.products.filter((product) => {
      if (kind === "subscription" && product.is_free) return false;
      if (kind === "freebies" && !product.is_free) return false;
      if (kind === "home" && state.categoryId && product.category_id !== state.categoryId) return false;
      if (!q) return true;
      const hay = `${product.name} ${product.sku} ${product.category || ""} ${product.description || ""}`.toLowerCase();
      return hay.includes(q);
    });
  }

  function detectBrand(product) {
    const text = `${product.name || ""} ${product.sku || ""} ${product.category || ""}`.toLowerCase();
    if (text.includes("playstation") || text.includes("psn") || text.includes("ps plus")) {
      return { cls: "brand-bg-playstation", name: "PlayStation.Plus", icon: "🎮" };
    }
    if (text.includes("xbox") || text.includes("game pass")) {
      return { cls: "brand-bg-xbox", name: "XBOX GAME PASS", icon: "🟢" };
    }
    if (text.includes("hulu")) {
      return { cls: "brand-bg-hulu", name: "hulu", icon: "📺" };
    }
    if (text.includes("adobe") || text.includes("creative cloud") || text.includes("photoshop")) {
      return { cls: "brand-bg-adobe", name: "Adobe Creative Cloud", icon: "🔴" };
    }
    if (text.includes("netflix")) {
      return { cls: "brand-bg-netflix", name: "NETFLIX 4K", icon: "🎬" };
    }
    if (text.includes("chatgpt") || text.includes("openai") || text.includes("gpt")) {
      return { cls: "brand-bg-chatgpt", name: "ChatGPT Plus", icon: "🤖" };
    }
    if (text.includes("canva")) {
      return { cls: "brand-bg-canva", name: "Canva Pro", icon: "🎨" };
    }
    if (text.includes("spotify")) {
      return { cls: "brand-bg-spotify", name: "Spotify", icon: "🎵" };
    }
    if (text.includes("nord") || text.includes("vpn")) {
      return { cls: "brand-bg-playstation", name: "NordVPN", icon: "🛡️" };
    }
    return { cls: "brand-bg-generic", name: product.name || "Digital License", icon: product.emoji || "⚡" };
  }

  function productCards(rows) {
    if (!rows.length) {
      if (!state.products.length) {
        return Array(6).fill(0).map(() => `<div class="skeleton-card"></div>`).join("");
      }
      return `<p class="empty" style="grid-column: 1 / -1; text-align: center; padding: 40px 20px; color: var(--muted);">No matching subscriptions found.</p>`;
    }

    return rows
      .map((product) => {
        const brand = detectBrand(product);
        const discount = discountPercent(product);
        const stockText = product.stock != null ? `${product.stock} Instant Keys Available` : (product.in_stock ? '24 Instant Keys Available' : 'Out of Stock');
        const headerContent = product.image_url
          ? `<img src="${escapeHtml(product.image_url)}" alt="${escapeHtml(product.name)}">`
          : `<div style="display: flex; flex-direction: column; align-items: center; gap: 4px;"><span style="font-size: 26px;">${brand.icon}</span><span style="font-weight: 900; font-size: 18px; text-shadow: 0 2px 8px rgba(0,0,0,0.5);">${escapeHtml(brand.name)}</span></div>`;

        return `
          <article class="showcase-card" data-sku="${escapeHtml(product.sku)}">
            <div class="showcase-brand-header ${brand.cls}">
              ${headerContent}
            </div>
            <div class="showcase-card-body">
              <h3 style="font-size: 15px; font-weight: 800; color: #fff; margin: 0; line-height: 1.3;">
                <a href="#product-${encodeURIComponent(product.sku)}" data-detail="${escapeHtml(product.sku)}" style="color: inherit; text-decoration: none;">
                  ${escapeHtml(product.name)}
                </a>
              </h3>
              <div class="showcase-stock-pill">
                <span class="showcase-stock-dot"></span>
                <span>${escapeHtml(stockText)}</span>
              </div>
              <div class="showcase-warranty-row">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg>
                <span>${escapeHtml(product.warranty_label || '12 Months Warranty')}</span>
              </div>
              <div class="showcase-price-row">
                <span class="showcase-price-bold">${usdPrice(product.sell_price)}</span>
                ${discount ? `<span class="showcase-discount-badge">-${discount}%</span>` : ''}
                ${discount ? `<span class="muted" style="text-decoration: line-through; font-size: 12px; margin-left: 2px;">${usdPrice(product.original_price)}</span>` : ''}
                <span class="price-pkr" style="font-size: 11.5px; margin-left: auto; color: var(--muted);">${pkrPrice(product.sell_price)}</span>
              </div>
              <button type="button" class="btn-get-access-glow" data-add="${escapeHtml(product.sku)}" ${product.in_stock ? '' : 'disabled'}>
                ${product.in_stock ? 'Get Access' : 'Out of Stock'}
              </button>
            </div>
          </article>
        `;
      })
      .join("");
  }

  function renderPills() {
    const counts = {};
    state.products.forEach((product) => {
      counts[product.category_id] = (counts[product.category_id] || 0) + 1;
    });
    els.pills.innerHTML = [
      `<button type="button" class="pill ${state.categoryId == null ? "active" : ""}" data-cat="">All</button>`,
      ...state.categories
        .filter((cat) => counts[cat.id])
        .map(
          (cat) =>
            `<button type="button" class="pill ${state.categoryId === cat.id ? "active" : ""}" data-cat="${cat.id}">${cat.emoji || ""} ${escapeHtml(cat.name)}</button>`
        ),
    ].join("");
  }

  function renderGrid() {
    els.grid.innerHTML = productCards(filteredProducts("home"));
  }

  function productBySku(sku) {
    return (
      state.products.find((row) => row.sku === sku) ||
      [...(state.featured.live || []), ...(state.featured.hot || []), ...(state.featured.best_seller || [])].find(
        (row) => row.sku === sku
      )
    );
  }

  function openSheet(el) {
    el.hidden = false;
  }

  function closeSheet(el) {
    el.hidden = true;
  }

  function renderProductSheet(product) {
    const descText = product.description || product.note || "";
    const displayDesc = descText ? `<div class="product-description-text" style="white-space: pre-wrap; margin-top:8px; line-height: 1.5;">${escapeHtml(descText)}</div>` : `<p class="muted" style="margin-top:8px;">No description available for this product.</p>`;

    els.productBody.innerHTML = `
      <div class="item-icon" style="width:100%;height:140px;font-size:40px;object-fit:cover;border-radius:12px;display:grid;place-items:center;background:#1c1730;overflow:hidden;margin-bottom:16px;">${
        product.image_url ? `<img src="${escapeHtml(product.image_url)}" style="width:100%;height:100%;object-fit:cover;" alt="">` : product.emoji || "🛍️"
      }</div>
      <h2 style="margin-top:16px;">${escapeHtml(product.name)}</h2>
      <h3 style="margin-top:20px;font-size:14px;color:var(--muted);text-transform:uppercase;letter-spacing:0.05em;">BEFORE YOU ORDER</h3>
      ${displayDesc}
    `;
    openSheet(els.productSheet);
  }

  function renderCartSheet() {
    if (!state.cart.length) {
      els.cartBody.innerHTML = `<p class="empty">Your cart is empty.</p>`;
      openSheet(els.cartSheet);
      return;
    }
    els.cartBody.innerHTML = `
      ${state.cart
        .map(
          (row) => `
        <div class="cart-row">
          <div>
            <strong>${escapeHtml(row.name)}</strong>
            <div class="muted">${formatPrice(row.sell_price)}</div>
          </div>
          <div class="cart-actions">
            <button type="button" class="qty-btn" data-sku="${row.sku}" data-delta="-1">−</button>
            ${row.qty}
            <button type="button" class="qty-btn" data-sku="${row.sku}" data-delta="1">+</button>
            <button type="button" class="remove-btn" data-remove="${row.sku}">Remove</button>
          </div>
        </div>
      `
        )
        .join("")}
      <p><strong>Total ${formatPrice(cartTotal())}</strong></p>
      <div class="hero-actions">
        <a class="btn btn-primary" href="#/checkout" id="cart-checkout">Direct checkout</a>
        <a class="btn btn-whatsapp" target="_blank" rel="noopener" href="${waHref()}">Order on WhatsApp</a>
      </div>
    `;
    document.getElementById("cart-checkout").onclick = () => closeSheet(els.cartSheet);
    openSheet(els.cartSheet);
  }

  function showView(id) {
    ["view-home", "view-collection", "view-auth", "view-checkout", "view-order"].forEach((key) => {
      const node = document.getElementById(key);
      if (node) node.hidden = key !== id;
    });
  }

  function renderCollection(kind) {
    const isFree = kind === "freebies";
    const badgeEl = document.getElementById("collection-badge");
    const titleEl = document.getElementById("collection-title");
    const subEl = document.getElementById("collection-sub");

    if (badgeEl) badgeEl.textContent = isFree ? "🎁 COMMUNITY DROPS" : "💎 PREMIUM SUBSCRIPTIONS";
    if (titleEl) titleEl.textContent = isFree ? "Freebies & Promo Drops" : "Active Subscription Plans";
    if (subEl) {
      subEl.textContent = isFree
        ? "Free tools, giveaway accounts, and starter access from the live SMF SHOP catalog."
        : "Paid plans, streaming accounts, and AI tool licenses with instant auto-delivery.";
    }

    if (isFree) {
      const items = filteredProducts("freebies");
      if (!items.length) {
        els.collectionGrid.innerHTML = `
          <div class="freebies-spotlight-card">
            <div class="freebies-icon-orb">🎁</div>
            <h2 class="freebies-title">Exclusive Live Community Drops</h2>
            <p class="freebies-desc">
              We drop free trial keys, bonus streaming credentials, and promotional tools directly on our official Telegram & WhatsApp channels. Stay connected to catch the next instant drop!
            </p>
            <div class="freebies-actions">
              <a class="btn btn-whatsapp" href="${waHref()}" target="_blank" rel="noopener">
                <span>💬</span> Claim on WhatsApp
              </a>
              <a class="btn btn-primary" href="/mini#catalog">
                <span>🛍️</span> Explore Premium Catalog
              </a>
            </div>
          </div>
        `;
      } else {
        els.collectionGrid.innerHTML = productCards(items);
      }
    } else {
      // Subscriptions
      if (!state.products.length) {
        els.collectionGrid.innerHTML = Array(6).fill(0).map(() => `<div class="skeleton-card"></div>`).join("");
      } else {
        const items = filteredProducts("subscription");
        const rows = items.length ? items : state.products;
        els.collectionGrid.innerHTML = productCards(rows);
      }
    }
    showView("view-collection");
  }

  function renderAuth() {
    const signed = Boolean(state.user && state.user.email);
    document.getElementById("auth-title").textContent = signed
      ? `Hi, ${state.user.name || "there"}`
      : "Create your SMF SHOP account";
    document.getElementById("signup-form").hidden = signed;
    document.getElementById("login-form").hidden = true;
    document.querySelector(".auth-switch").hidden = signed;
    const signedActions = document.getElementById("auth-signed-actions");
    if (signedActions) signedActions.hidden = !signed;
    const logout = document.getElementById("btn-logout");
    if (logout) logout.hidden = !signed;
    const lede = els.viewAuth.querySelector(".lede");
    if (lede) {
      lede.textContent = signed
        ? "You are signed in to your SMF SHOP customer account."
        : "Sign up with email — no Telegram login.";
    }
    showView("view-auth");
  }

  function renderCheckout() {
    const box = document.getElementById("checkout-items");
    if (!state.cart.length) {
      box.innerHTML = `<p class="empty">Your cart is empty. Add a product first.</p>`;
      document.getElementById("checkout-form").hidden = true;
      showView("view-checkout");
      return;
    }
    document.getElementById("checkout-form").hidden = false;
    box.innerHTML = state.cart
      .map((row) => `<div class="cart-row"><div><strong>${escapeHtml(row.name)}</strong><div class="muted">${row.qty} × ${formatPrice(row.sell_price)}</div></div><strong>${formatPrice(row.sell_price * row.qty)}</strong></div>`)
      .join("") + `<p><strong>Total ${formatPrice(cartTotal())}</strong></p>`;
    if (state.user) {
      document.getElementById("checkout-name").value = state.user.name || "";
      document.getElementById("checkout-email").value = state.user.email || "";
    }
    document.getElementById("payment-methods").innerHTML = (state.methods || [])
      .map(
        (method, index) => `
        <label class="pay-option">
          <input type="radio" name="payment_method" value="${escapeHtml(method.code)}" ${index === 0 ? "checked" : ""}>
          <span>
            <strong>${method.icon || "💳"} ${escapeHtml(method.name)}</strong>
            ${method.network ? `<div class="muted">${escapeHtml(method.network)}</div>` : ""}
            ${method.instructions ? `<div class="muted">${escapeHtml(method.instructions)}</div>` : ""}
          </span>
        </label>
      `
      )
      .join("") || `<p class="empty">No payment methods are configured yet.</p>`;
    if (els.whatsappCheckout) els.whatsappCheckout.href = waHref();
    showView("view-checkout");
  }

  function renderOrder(payload) {
    const first = (payload.orders && payload.orders[0]) || payload;
    const pay =
      payload.payment_method && typeof payload.payment_method === "object"
        ? payload.payment_method
        : {
            name: payload.method_name || payload.payment_method || first.payment_method || "",
            address: payload.pay_to || "",
            instructions: payload.instructions || "",
          };
    document.getElementById("order-title").textContent = `Order ${first.order_code}`;
    document.getElementById("order-body").innerHTML = `
      <p>Your order is <strong>${escapeHtml(first.status || "pending")}</strong>. Pay with <strong>${escapeHtml(pay.name || first.payment_method || "")}</strong> using the details below, then wait for admin confirmation — same flow as the Telegram shop.</p>
      ${(payload.orders || [first])
        .map((row) => `<div class="cart-row"><div><strong>${escapeHtml(row.name || row.product || "")}</strong><div class="muted">${row.qty} × ${row.sku || ""}</div></div><strong>${formatPrice(row.amount)}</strong></div>`)
        .join("")}
      <p><strong>Total ${formatPrice(payload.total || first.amount)}</strong></p>
      ${pay.address ? `<p>Send to: <code>${escapeHtml(pay.address)}</code></p>` : ""}
      ${pay.instructions ? `<p class="muted">${escapeHtml(pay.instructions)}</p>` : ""}
      <div class="hero-actions">
        <a class="btn btn-whatsapp" target="_blank" rel="noopener" href="${waHref()}">Order on WhatsApp</a>
        <a class="btn btn-primary" href="#/">Back to shop</a>
      </div>
    `;
    showView("view-order");
  }

  function applyRoute() {
    state.route = currentPath();
    renderChrome();
    if (state.route.startsWith("/order/")) {
      const code = decodeURIComponent(state.route.slice("/order/".length));
      if (state.order && (state.order.order_code === code || (state.order.orders || []).some((row) => row.order_code === code))) {
        renderOrder(state.order);
        return;
      }
      showView("view-order");
      document.getElementById("order-title").textContent = "Loading order…";
      document.getElementById("order-body").innerHTML = `<p class="empty">Loading ${escapeHtml(code)}…</p>`;
      getJSON(`/api/web/orders/${encodeURIComponent(code)}`)
        .then((data) => {
          state.order = data;
          renderOrder(data);
        })
        .catch((err) => {
          document.getElementById("order-title").textContent = "Order not found";
          document.getElementById("order-body").innerHTML = `<p class="empty">${escapeHtml(err.message)}</p>`;
        });
      return;
    }
    if (state.route === "/account") {
      window.location.href = "/account";
      return;
    }
    if (state.route === "/subscription" || state.route === "/subscriptions") renderCollection("subscription");
    else if (state.route === "/freebies") renderCollection("freebies");
    else if (state.route === "/signup" || state.route === "/login") renderAuth();
    else if (state.route === "/checkout") renderCheckout();
    else {
      showView("view-home");
      renderFeatured();
      renderPills();
      renderGrid();
    }
    window.scrollTo({ top: 0 });
  }

  document.addEventListener("click", (event) => {
    const currencyPick = event.target.closest("[data-currency]");
    if (currencyPick) {
      state.currency = currencyPick.dataset.currency;
      localStorage.setItem("smf_currency", state.currency);
      closeMenus();
      applyRoute();
      return;
    }
    const languagePick = event.target.closest("[data-language]");
    if (languagePick) {
      state.language = languagePick.dataset.language;
      localStorage.setItem("smf_language", state.language);
      closeMenus();
      renderChrome();
      return;
    }
    if (event.target.closest("#currency-btn")) {
      const open = els.currencyMenu.hidden;
      closeMenus();
      els.currencyMenu.hidden = !open;
      els.currencyBtn.setAttribute("aria-expanded", String(open));
      return;
    }
    if (event.target.closest("#language-btn")) {
      const open = els.languageMenu.hidden;
      closeMenus();
      els.languageMenu.hidden = !open;
      els.languageBtn.setAttribute("aria-expanded", String(open));
      return;
    }
    if (event.target.closest("#btn-close-perks")) {
      const perks = document.getElementById("floating-perks-widget");
      if (perks) perks.classList.add("minimized");
      return;
    }
    if (event.target.closest("#search-submit-btn")) {
      const input = document.getElementById("search-input");
      if (input) {
        state.query = input.value.trim();
        renderGrid();
      }
      return;
    }

    const remove = event.target.closest("[data-remove]");
    if (remove) {
      removeFromCart(remove.dataset.remove);
      renderCartSheet();
      return;
    }

    const qty = event.target.closest(".qty-btn");
    if (qty) {
      const row = state.cart.find((item) => item.sku === qty.dataset.sku);
      if (row) {
        row.qty += Number(qty.dataset.delta);
        if (row.qty <= 0) removeFromCart(row.sku);
        else saveCart();
        renderCartSheet();
      }
      return;
    }

    const addBtn = event.target.closest("[data-add]");
    if (addBtn) {
      const product = productBySku(addBtn.dataset.add);
      if (product) {
        addToCart(product);
        renderCartSheet();
      }
      return;
    }

    const detailBtn = event.target.closest("[data-detail]");
    if (detailBtn) {
      event.preventDefault();
      const product = productBySku(detailBtn.dataset.detail);
      if (product) renderProductSheet(product);
      return;
    }

    const skuBtn = event.target.closest("[data-sku]");
    if (skuBtn && skuBtn.dataset.sku) {
      const product = productBySku(skuBtn.dataset.sku);
      if (product) renderProductSheet(product);
      return;
    }

    const cat = event.target.closest("[data-cat]");
    if (cat) {
      state.categoryId = cat.dataset.cat ? Number(cat.dataset.cat) : null;
      renderPills();
      renderGrid();
      return;
    }

    const infoBtn = event.target.closest("[data-info]");
    if (infoBtn) {
      // Find the specific popover next to THIS button to avoid duplicate ID issues
      const targetPopover = infoBtn.parentElement.querySelector(".quick-details-popover");

      document.querySelectorAll('.quick-details-popover').forEach(p => {
        if (p !== targetPopover) p.hidden = true;
      });
      if (targetPopover) {
        targetPopover.hidden = !targetPopover.hidden;
      }
      return;
    }

    if (!event.target.closest("[data-info]") && !event.target.closest(".quick-details-popover")) {
      document.querySelectorAll('.quick-details-popover').forEach(p => {
        p.hidden = true;
      });
    }

    if (event.target.closest("[data-close]")) {
      closeSheet(event.target.closest(".sheet"));
    }
    if (!event.target.closest(".dropdown")) closeMenus();
  });

  // Keyboard Shortcuts: Escape to close popovers, ⌘K / Ctrl+K to focus search
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      document.querySelectorAll('.quick-details-popover').forEach(p => p.hidden = true);
    }
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
      e.preventDefault();
      const searchInput = document.getElementById("search-input");
      if (searchInput) {
        searchInput.focus();
        searchInput.select();
      }
    }
  });

  const btnCart = document.getElementById("btn-cart");
  if (btnCart) btnCart.onclick = () => renderCartSheet();

  const btnSupport = document.getElementById("btn-support");
  if (btnSupport) {
    btnSupport.onclick = () => {
      const href = waHref();
      if (href && href !== "#") window.open(href, "_blank", "noopener");
    };
  }

  if (els.search) {
    els.search.addEventListener("input", () => {
      state.query = els.search.value;
      if (state.route === "/subscription" || state.route === "/subscriptions") renderCollection("subscription");
      else if (state.route === "/freebies") renderCollection("freebies");
      else renderGrid();
    });
  }

  const btnExplore = document.getElementById("btn-explore");
  if (btnExplore) {
    btnExplore.addEventListener("click", (event) => {
      event.preventDefault();
      location.hash = "#/";
      const catalogEl = document.getElementById("catalog");
      if (catalogEl) setTimeout(() => catalogEl.scrollIntoView({ behavior: "smooth" }), 50);
    });
  }

  // Capsule Navigation Buttons (Categorys, Brands, Products)
  const navCategories = document.getElementById("nav-categories");
  if (navCategories) {
    navCategories.addEventListener("click", () => {
      document.querySelectorAll(".nav-capsule-btn").forEach(b => b.classList.remove("active"));
      navCategories.classList.add("active");
      const pills = document.getElementById("category-pills");
      if (pills) pills.scrollIntoView({ behavior: "smooth", block: "center" });
    });
  }

  const navBrands = document.getElementById("nav-brands");
  if (navBrands) {
    navBrands.addEventListener("click", () => {
      document.querySelectorAll(".nav-capsule-btn").forEach(b => b.classList.remove("active"));
      navBrands.classList.add("active");
      const catalog = document.getElementById("catalog");
      if (catalog) catalog.scrollIntoView({ behavior: "smooth", block: "start" });
    });
  }

  const navProducts = document.getElementById("nav-products");
  if (navProducts) {
    navProducts.addEventListener("click", () => {
      document.querySelectorAll(".nav-capsule-btn").forEach(b => b.classList.remove("active"));
      navProducts.classList.add("active");
      const grid = document.getElementById("product-grid");
      if (grid) grid.scrollIntoView({ behavior: "smooth", block: "start" });
    });
  }

  const showLoginBtn = document.getElementById("show-login");
  if (showLoginBtn) {
    showLoginBtn.onclick = () => {
      const signupForm = document.getElementById("signup-form");
      const loginForm = document.getElementById("login-form");
      const authTitle = document.getElementById("auth-title");
      if (signupForm) signupForm.hidden = true;
      if (loginForm) loginForm.hidden = false;
      if (authTitle) authTitle.textContent = "Log in to SMF SHOP";
    };
  }

  const logoutBtn = document.getElementById("btn-logout");
  if (logoutBtn) {
    logoutBtn.onclick = async () => {
      try {
        await postJSON("/api/web/logout", {});
      } catch (_err) {}
      saveUser(null);
      location.hash = "#/signup";
      renderAuth();
    };
  }

  document.getElementById("signup-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const error = document.getElementById("signup-error");
    error.hidden = true;
    const payload = Object.fromEntries(new FormData(form).entries());
    if (payload.password !== payload.confirm) {
      error.textContent = "Passwords do not match.";
      error.hidden = false;
      return;
    }
    try {
      const data = await postJSON("/api/web/signup", {
        name: payload.name,
        email: payload.email,
        password: payload.password,
      });
      saveUser(data.user);
      if (state.cart.length) {
        location.hash = "#/checkout";
      } else {
        window.location.href = "/account";
      }
    } catch (err) {
      error.textContent = err.message;
      error.hidden = false;
    }
  });

  document.getElementById("login-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const error = document.getElementById("login-error");
    error.hidden = true;
    const payload = Object.fromEntries(new FormData(event.currentTarget).entries());
    try {
      const data = await postJSON("/api/web/login", payload);
      saveUser(data.user);
      if (state.cart.length) {
        location.hash = "#/checkout";
      } else {
        window.location.href = "/account";
      }
    } catch (err) {
      error.textContent = err.message;
      error.hidden = false;
    }
  });

  document.getElementById("checkout-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const error = document.getElementById("checkout-error");
    error.hidden = true;
    const form = event.currentTarget;
    const method = (form.payment_method && form.payment_method.value) || "";
    try {
      const data = await postJSON("/api/web/checkout", {
        name: document.getElementById("checkout-name").value,
        email: document.getElementById("checkout-email").value,
        payment_method: method,
        items: state.cart.map((row) => ({ sku: row.sku, qty: row.qty })),
      });
      saveUser(data.user);
      state.cart = [];
      saveCart();
      state.order = data;
      location.hash = `#/order/${data.order_code}`;
      renderOrder(data);
    } catch (err) {
      error.textContent = err.message;
      error.hidden = false;
    }
  });

  window.addEventListener("hashchange", applyRoute);

  // Instant 0ms Paint: render immediately from cache or display skeletons
  renderChrome();
  if (state.products.length) {
    renderFeatured();
    renderPills();
    applyRoute();
  } else {
    // Render high-tech skeleton placeholders so page is never blank
    els.grid.innerHTML = Array.from({ length: 6 })
      .map(
        () => `
        <div class="skeleton-card">
          <div class="skeleton-shimmer"></div>
        </div>
      `
      )
      .join("");
    applyRoute();
  }

  // Progressive Non-Blocking Asynchronous Data Hydration:
  getJSON("/api/web/shop")
    .then((shop) => {
      state.shop = shop;
      try { localStorage.setItem("smf_cache_shop", JSON.stringify(shop)); } catch (_) {}
      renderChrome();
    })
    .catch(() => {});

  getJSON("/api/web/me")
    .then((authRes) => {
      if (authRes && authRes.authenticated && authRes.user) {
        saveUser(authRes.user);
      } else {
        saveUser(null);
      }
    })
    .catch(() => {});

  getJSON("/api/web/categories")
    .then((categories) => {
      state.categories = categories;
      try { localStorage.setItem("smf_cache_categories", JSON.stringify(categories)); } catch (_) {}
      renderPills();
    })
    .catch(() => {});

  getJSON("/api/web/featured")
    .then((featured) => {
      state.featured = featured;
      try { localStorage.setItem("smf_cache_featured", JSON.stringify(featured)); } catch (_) {}
      renderFeatured();
    })
    .catch(() => {});

  getJSON("/api/web/products")
    .then((products) => {
      state.products = products;
      try { localStorage.setItem("smf_cache_products", JSON.stringify(products)); } catch (_) {}
      renderPills();
      renderGrid();
      const path = currentPath();
      if (path === "/subscription" || path === "/subscriptions" || path === "/freebies") {
        renderCollection(path.includes("freebie") ? "freebies" : "subscription");
      }
    })
    .catch((err) => {
      if (!state.products.length) {
        els.grid.innerHTML = `<p class="empty">Could not load catalog (${escapeHtml(err.message)}).</p>`;
      }
    });

  getJSON("/api/web/payment-methods")
    .then((methods) => {
      state.methods = methods;
    })
    .catch(() => []);
})();
