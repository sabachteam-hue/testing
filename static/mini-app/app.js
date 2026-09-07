(() => {
  function initTelegram() {
    const tg = window.Telegram && window.Telegram.WebApp;
    if (tg && typeof tg.ready === "function") {
      try {
        tg.ready();
        tg.expand();
        tg.setHeaderColor("#09070f");
        tg.setBackgroundColor("#09070f");
      } catch (_err) {
        /* older clients */
      }
    }
  }
  initTelegram();
  if (!window.Telegram || !window.Telegram.WebApp) {
    window.addEventListener("load", initTelegram, { once: true });
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
    currency: localStorage.getItem("smf_currency") || "PKR",
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
    if (state.currency === "EUR") {
      const eur = value * 0.92;
      return `€${eur.toFixed(2)}`;
    }
    if (state.currency === "GBP") {
      const gbp = value * 0.79;
      return `£${gbp.toFixed(2)}`;
    }
    if (state.currency === "INR") {
      const inr = value * 83.5;
      return `₹${inr.toLocaleString("en-IN", { maximumFractionDigits: 0 })}`;
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

  // Smart Avatar Generator based on name gender guessing
  function getSmartAvatar(name, isSignOut = false) {
    if (isSignOut || !name) {
      return `<svg class="vip-user-signout-icon" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="color: #c4b5fd; display: block;"><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/></svg>`;
    }
    const clean = String(name).toLowerCase().trim();
    const femalePatterns = [
      'saba', 'fatima', 'ayesha', 'aisha', 'zainab', 'sana', 'maryam', 'maria', 
      'hira', 'anaya', 'sarah', 'sara', 'samina', 'nadia', 'bushra', 'sidra', 
      'iqra', 'nimra', 'sadia', 'rabia', 'mehwish', 'komal', 'noor', 'amna', 
      'faryal', 'mahnoor', 'laiba', 'kinza', 'iman', 'emaan', 'aleena', 'alina',
      'kiran', 'asma', 'shazia', 'nida', 'zoya', 'mishal', 'hina', 'farah',
      'female', 'woman', 'girl', 'lady', 'miss', 'mrs'
    ];
    const malePatterns = [
      'ali', 'ahmed', 'ahmad', 'salman', 'usman', 'hamza', 'bilal', 'saeed', 
      'omar', 'umer', 'umar', 'hassan', 'hasan', 'hussain', 'hussein', 'fahad', 
      'waqas', 'shahid', 'alex', 'john', 'david', 'michael', 'muhammad', 'mohammad', 
      'asif', 'kashif', 'irfan', 'kamran', 'adnan', 'tariq', 'rashid', 'naveed',
      'imran', 'babur', 'rizwan', 'shaheen', 'haris', 'naseem', 'shadab',
      'male', 'man', 'boy', 'mr'
    ];
    
    let gender = 'neutral';
    const tokens = clean.split(/[\s._-]+/);
    for (const token of tokens) {
      if (femalePatterns.includes(token)) { gender = 'female'; break; }
      if (malePatterns.includes(token)) { gender = 'male'; break; }
    }
    if (gender === 'neutral') {
      for (const p of femalePatterns) {
        if (clean.includes(p)) { gender = 'female'; break; }
      }
    }
    if (gender === 'neutral') {
      for (const p of malePatterns) {
        if (clean.includes(p)) { gender = 'male'; break; }
      }
    }

    if (gender === 'female') {
      return `<svg class="vip-user-avatar-svg" viewBox="0 0 36 36" width="28" height="28" style="border-radius: 50%; border: 1.5px solid #c4b5fd; box-shadow: 0 0 10px rgba(167, 139, 250, 0.6); display: block;">
        <circle cx="18" cy="18" r="18" fill="#4c1d95"/>
        <path d="M10 18 C10 10 26 10 26 18 C26 23 25 27 24 30 C20 31 16 31 12 30 C11 27 10 23 10 18 Z" fill="#2e1065"/>
        <ellipse cx="18" cy="17" rx="5.5" ry="6.5" fill="#fed7aa"/>
        <path d="M11 15 C13 11 23 11 25 15 C23 13 19 13 18 15 C17 13 13 13 11 15 Z" fill="#1e1b4b"/>
        <path d="M9 34 C9 26 27 26 27 34 Z" fill="#f472b6"/>
      </svg>`;
    }

    if (gender === 'male') {
      return `<svg class="vip-user-avatar-svg" viewBox="0 0 36 36" width="28" height="28" style="border-radius: 50%; border: 1.5px solid #8b5cf6; box-shadow: 0 0 10px rgba(139, 92, 246, 0.6); display: block;">
        <circle cx="18" cy="18" r="18" fill="#312e81"/>
        <path d="M12 15 C12 9 24 9 24 15 C23 11 19 11 18 12 C17 11 13 11 12 15 Z" fill="#0f172a"/>
        <ellipse cx="18" cy="17" rx="5.2" ry="6" fill="#fed7aa"/>
        <path d="M8 34 C8 26 28 26 28 34 Z" fill="#818cf8"/>
      </svg>`;
    }

    return `<svg class="vip-user-avatar-svg" viewBox="0 0 36 36" width="28" height="28" style="border-radius: 50%; border: 1.5px solid #c084fc; box-shadow: 0 0 8px rgba(192, 132, 252, 0.5); display: block;">
      <circle cx="18" cy="18" r="18" fill="#581c87"/>
      <circle cx="18" cy="14" r="5" fill="#fed7aa"/>
      <path d="M10 32 C10 24 26 24 26 32 Z" fill="#c084fc"/>
    </svg>`;
  }

  function waHref(product) {
    const base = (state.shop && state.shop.whatsapp_url) || "https://wa.me/";
    let text = "Hi SMF SHOP, I want to place an order from the Mini App.";
    if (product) {
      text = `Hi SMF SHOP, I want to order ${product.name} (${product.sku}) for ${formatPrice(product.sell_price)}`;
    } else if (state.cart.length) {
      const lines = state.cart.map((row) => `${row.qty}x ${row.name} (${formatPrice(row.sell_price)})`).join(", ");
      text = `Hi SMF SHOP, I want to order: ${lines}. Total ${formatPrice(cartTotal())}`;
    }
    const sep = base.includes("?") ? "&" : "?";
    return `${base}${sep}text=${encodeURIComponent(text)}`;
  }

  function closeMenus() {
    document.querySelectorAll(".menu").forEach((menu) => {
      menu.hidden = true;
    });
    document.querySelectorAll(".chip").forEach((btn) => btn.setAttribute("aria-expanded", "false"));
    const authDd = document.getElementById("meetway-auth-dropdown");
    if (authDd) authDd.hidden = true;
  }

  function flagIso(item) {
    return (item && (item.flag_iso || FLAG_ISO[item.code])) || "xx";
  }

  function flagMarkup(item) {
    const iso = flagIso(item);
    const label = escapeHtml(item.label || item.name || item.code || "");
    return `<img class="flag-img" src="/static/mini-app/flags/${iso}.svg" alt="" width="20" height="14" onerror="this.onerror=null;this.src='/static/mini-app/flags/xx.svg'"> ${label}`;
  }

  const CURRENCIES = [
    { code: "USD", label: "USD ($)", flag_iso: "us" },
    { code: "PKR", label: "PKR (Rs.)", flag_iso: "pk" },
    { code: "EUR", label: "EUR (€)", flag_iso: "eu" },
    { code: "GBP", label: "GBP (£)", flag_iso: "gb" },
    { code: "INR", label: "INR (₹)", flag_iso: "in" },
  ];

  function renderMenus() {
    const currencies = CURRENCIES;
    if (els.currencyMenu) {
      els.currencyMenu.innerHTML = currencies
        .map(
          (item) => `
        <button type="button" role="menuitem" data-currency="${item.code}" class="${item.code === state.currency ? "active" : ""}">
          <img class="flag-img" src="/static/mini-app/flags/${item.flag_iso}.svg" alt="" width="20" height="14" onerror="this.onerror=null;this.src='/static/mini-app/flags/xx.svg'">
          <span>${item.label}</span>
          ${item.code === state.currency ? '<span class="check-indicator">✓</span>' : ""}
        </button>
      `
        )
        .join("");
    }
    const current = currencies.find((item) => item.code === state.currency) || currencies[1] || currencies[0];
    if (els.currencyBtn) {
      els.currencyBtn.innerHTML = `
        <span class="flag-icon" id="currency-flag"><img class="flag-img" src="/static/mini-app/flags/${current.flag_iso}.svg" alt="" width="20" height="14"></span>
        <span id="currency-label">${current.label}</span>
        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="m6 9 6 6 6-6"/></svg>
      `;
    }

    const languages = (state.shop && state.shop.languages) || [{ code: "en", name: "English", flag_iso: "gb" }];
    if (els.languageMenu) {
      els.languageMenu.innerHTML = languages
        .map((item) => `<button type="button" data-language="${item.code}">${flagMarkup(item)}</button>`)
        .join("");
    }
    const lang = languages.find((item) => item.code === state.language) || languages[0];
    state.language = lang.code;
    if (els.languageBtn) {
      els.languageBtn.innerHTML = flagMarkup(lang);
    }
  }

  function renderHeroBadges() {
    if (!state.shop || !state.shop.hero_logos) return;
    const logos = state.shop.hero_logos;
    if (logos.netflix) {
      const el = document.getElementById("hero-badge-netflix");
      if (el) el.innerHTML = `<img src="${escapeHtml(logos.netflix)}" class="hero-custom-logo-img" alt="Netflix">`;
    }
    if (logos.discord) {
      const el = document.getElementById("hero-badge-discord");
      if (el) el.innerHTML = `<img src="${escapeHtml(logos.discord)}" class="hero-custom-logo-img" alt="Discord">`;
    }
    if (logos.chatgpt) {
      const el = document.getElementById("hero-badge-chatgpt");
      if (el) el.innerHTML = `<img src="${escapeHtml(logos.chatgpt)}" class="hero-custom-logo-img" alt="ChatGPT">`;
      const el2 = document.getElementById("hero-badge-chatgpt-r");
      if (el2) el2.innerHTML = `<img src="${escapeHtml(logos.chatgpt)}" class="hero-custom-logo-img" alt="ChatGPT">`;
    }
    if (logos.star) {
      const el = document.getElementById("hero-badge-star");
      if (el) el.innerHTML = `<img src="${escapeHtml(logos.star)}" class="hero-custom-logo-img" alt="VIP Star">`;
    }
    if (logos.canva) {
      const el = document.getElementById("hero-badge-canva");
      if (el) el.innerHTML = `<img src="${escapeHtml(logos.canva)}" class="hero-custom-logo-img" alt="Canva">`;
    }
    if (logos.spotify) {
      const el = document.getElementById("hero-badge-spotify-green");
      if (el) el.innerHTML = `<img src="${escapeHtml(logos.spotify)}" class="hero-custom-logo-img" alt="Spotify">`;
    }
  }

  function renderChrome() {
    renderHeroBadges();
    if (state.shop) {
      if (els.eyebrow && state.shop.eyebrow && state.shop.eyebrow !== "PREMIUM DIGITAL ACCOUNTS") els.eyebrow.textContent = state.shop.eyebrow;
      if (els.headline) els.headline.textContent = "GET PREMIUM TOOLS. PAY LESS. DO MORE.";
      if (els.tagline && state.shop.tagline) els.tagline.textContent = state.shop.tagline;
      [els.whatsapp, els.whatsappCatalog, els.whatsappCheckout].forEach((node) => {
        if (!node) return;
        node.href = waHref();
        node.hidden = false;
        node.style.display = "";
      });
    }
    const signed = Boolean(state.user && state.user.email);
    const balanceWidget = document.getElementById("btn-balance-link");
    const currencyDd = document.getElementById("currency-dd");
    const languageDd = document.getElementById("language-dd");
    const accountBtn = document.getElementById("btn-account");
    const accountLabel = document.getElementById("account-pill-label");

    // When logged in: show Live Balance. When logged out: hide Live Balance, show currency dropdown
    if (balanceWidget) {
      balanceWidget.style.display = signed ? "inline-flex" : "none";
    }
    if (currencyDd) {
      currencyDd.style.display = signed ? "none" : "inline-block";
    }
    if (languageDd) {
      languageDd.style.display = signed ? "none" : "inline-block";
    }

    const perksWidget = document.getElementById("floating-perks-widget");
    if (perksWidget) {
      perksWidget.hidden = !signed;
      perksWidget.style.display = signed ? "block" : "none";
    }

    if (accountBtn) {
      accountBtn.href = signed ? "/account" : "#/signup";
      if (accountLabel) {
        accountLabel.textContent = signed ? (state.user.name || "VIP") : "Sign In";
      }
    }
    const topbarAvatar = document.getElementById("topbar-user-avatar");
    if (topbarAvatar) {
      topbarAvatar.innerHTML = getSmartAvatar(signed ? (state.user.name || state.user.email || "") : "", !signed);
    }
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

    const activeRoute = (state.route || "").replace(/^#/, "");
    const hotBtn = document.getElementById("nav-hot-deals");
    const stockBtn = document.getElementById("nav-live-stock");
    const prodsBtn = document.getElementById("nav-all-products");
    if (hotBtn) hotBtn.classList.toggle("active", activeRoute === "/hot-deals" || activeRoute === "/deals");
    if (stockBtn) stockBtn.classList.toggle("active", activeRoute === "/live-stock" || activeRoute === "/stock");
    if (prodsBtn) prodsBtn.classList.toggle("active", activeRoute === "/products" || activeRoute === "/all-products");

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
      if (kind === "hot-deals") {
        const isDiscounted = (product.original_price && Number(product.original_price) > Number(product.sell_price)) || (product.discount_percent && Number(product.discount_percent) > 0);
        const isHot = (state.featured && state.featured.hot && state.featured.hot.some((h) => h.sku === product.sku)) || (state.featured && state.featured.best_seller && state.featured.best_seller.some((b) => b.sku === product.sku));
        if (!isDiscounted && !isHot) return false;
      }
      if (kind === "live-stock") {
        const hasStock = Boolean(product.in_stock && product.availability !== "out_of_stock" && (product.stock == null || Number(product.stock) > 0));
        if (!hasStock) return false;
      }
      // "products" returns all products (both in-stock and out-of-stock)
      if (kind === "home" && state.categoryId != null) {
        const catObj = (state.categories || []).find((c) => c.id === state.categoryId);
        const matchId = product.category_id === state.categoryId;
        const matchName = Boolean(catObj && product.category && product.category.toLowerCase() === catObj.name.toLowerCase());
        if (!matchId && !matchName) return false;
      }
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
          <article class="showcase-card product-card" data-sku="${escapeHtml(product.sku)}">
            <div class="showcase-brand-header ${brand.cls}">
              ${headerContent}
            </div>
            <div class="showcase-card-body">
              <h3 style="font-size: 15px; font-weight: 800; color: #fff; margin: 0; line-height: 1.3;">
                <a href="#product-${encodeURIComponent(product.sku)}" data-detail="${escapeHtml(product.sku)}" style="color: inherit; text-decoration: none;">
                  ${escapeHtml(product.name)}
                </a>
              </h3>
              <a href="#product-${encodeURIComponent(product.sku)}" data-detail="${escapeHtml(product.sku)}" class="view-desc-link" style="color: var(--cyan); font-size: 11px; font-weight: 700; text-decoration: none; display: inline-flex; align-items: center; gap: 3px; margin-top: 4px;">
                <span>VIEW DESCRIPTION</span> →
              </a>
              <div class="showcase-stock-pill" style="margin-top: 6px;">
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
              <button type="button" class="btn-get-access-glow btn-add-cart" data-add="${escapeHtml(product.sku)}" ${product.in_stock ? '' : 'disabled'}>
                ${product.in_stock ? '⚡ Get Access · Add to Cart' : 'Out of Stock'}
              </button>
            </div>
          </article>
        `;
      })
      .join("");
  }

  const DEFAULT_CATEGORIES = [
    { id: 1, name: "CapCut", emoji: "CC" },
    { id: 2, name: "ChatGPT", emoji: "AI" },
    { id: 3, name: "Netflix", emoji: "TV" },
    { id: 4, name: "Social Media", emoji: "SM" },
    { id: 5, name: "VPN & Security", emoji: "🛡️" },
    { id: 6, name: "Gaming & Keys", emoji: "🎮" },
  ];

  function renderCategoryIcon(iconVal, categoryName) {
    const raw = String(iconVal || "").trim();
    const name = String(categoryName || "").toLowerCase();
    const code = raw.toUpperCase();

    if (raw.startsWith("http://") || raw.startsWith("https://") || raw.startsWith("/") || raw.includes(".png") || raw.includes(".svg") || raw.includes(".webp")) {
      return `<img src="${escapeHtml(raw)}" class="cat-pill-img" alt="" onerror="this.outerHTML='<span class=\\'cat-pill-icon\\'>📦</span>'">`;
    }

    if (code === "CC" || name.includes("capcut") || name.includes("video")) {
      return '<span class="cat-pill-icon">🎬</span>';
    }
    if (code === "AI" || name.includes("chatgpt") || name.includes("openai") || name.includes("gpt") || name.includes("ai")) {
      return '<span class="cat-pill-icon">🤖</span>';
    }
    if (code === "TV" || name.includes("netflix") || name.includes("streaming") || name.includes("ott")) {
      return '<span class="cat-pill-icon">📺</span>';
    }
    if (code === "SM" || name.includes("social") || name.includes("telegram") || name.includes("instagram")) {
      return '<span class="cat-pill-icon">🌐</span>';
    }
    if (name.includes("vpn") || name.includes("security")) {
      return '<span class="cat-pill-icon">🛡️</span>';
    }
    if (name.includes("game") || name.includes("gaming") || name.includes("xbox") || name.includes("psn")) {
      return '<span class="cat-pill-icon">🎮</span>';
    }
    if (name.includes("music") || name.includes("spotify") || name.includes("audio")) {
      return '<span class="cat-pill-icon">🎵</span>';
    }
    if (name.includes("canva") || name.includes("adobe") || name.includes("design")) {
      return '<span class="cat-pill-icon">🎨</span>';
    }

    if (raw) {
      return `<span class="cat-pill-icon">${escapeHtml(raw)}</span>`;
    }
    return '<span class="cat-pill-icon">📦</span>';
  }

  function renderPills() {
    if (!els.pills) return;
    const cats = (state.categories && state.categories.length) ? state.categories : DEFAULT_CATEGORIES;
    const isAll = state.categoryId == null;
    els.pills.innerHTML = [
      `<button type="button" class="pill ${isAll ? "active" : ""}" data-cat=""><span class="cat-pill-icon">🛍️</span> <span>All Products</span></button>`,
      ...cats.map((cat) => {
        const isActive = state.categoryId === cat.id;
        const iconMarkup = renderCategoryIcon(cat.emoji, cat.name);
        return `<button type="button" class="pill ${isActive ? "active" : ""}" data-cat="${cat.id}">${iconMarkup} <span>${escapeHtml(cat.name)}</span></button>`;
      }),
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
    if (!el) return;
    el.hidden = false;
    requestAnimationFrame(() => {
      el.classList.add("sheet-open");
    });
  }

  function closeSheet(el) {
    if (!el) return;
    el.classList.remove("sheet-open");
    setTimeout(() => {
      el.hidden = true;
    }, 200);
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
      els.cartBody.innerHTML = `
        <div style="text-align: center; padding: 48px 16px; color: var(--muted);">
          <div style="font-size: 48px; margin-bottom: 14px;">🛍️</div>
          <h3 style="color: #fff; margin-bottom: 8px; font-size: 18px;">Your cart is empty</h3>
          <p style="font-size: 13px; margin: 0 0 20px 0;">Add digital licenses from the store to checkout.</p>
          <button type="button" class="cart-btn-primary" style="max-width: 200px; margin: 0 auto;" id="btn-empty-browse">Browse Products</button>
        </div>
      `;
      const browseBtn = document.getElementById("btn-empty-browse");
      if (browseBtn) browseBtn.onclick = () => closeSheet(els.cartSheet);
      openSheet(els.cartSheet);
      return;
    }

    const isSigned = Boolean(state.user && state.user.email);
    const subtotal = cartTotal();
    const total = subtotal;

    const itemsHtml = `
      <div class="cart-items-list">
        ${state.cart
          .map((row) => {
            const prod = productBySku(row.sku);
            const thumb = (prod && prod.image_url)
              ? `<img src="${escapeHtml(prod.image_url)}" alt="">`
              : (row.emoji || (prod && prod.emoji) || "⚡");
            const subtitle = (prod && (prod.subtitle || prod.category || prod.duration)) || "Instant Delivery · Team Plan";

            return `
              <div class="cart-item-card">
                <div class="cart-item-thumb">${thumb}</div>
                <div class="cart-item-info">
                  <div class="cart-item-name">${escapeHtml(row.name)}</div>
                  <div class="cart-item-meta">${escapeHtml(subtitle)}</div>
                  <div class="cart-qty-pill">
                    <button type="button" class="qty-btn-inline" data-cart-delta="-1" data-sku="${escapeHtml(row.sku)}">−</button>
                    <span class="cart-qty-val">${row.qty}</span>
                    <button type="button" class="qty-btn-inline" data-cart-delta="1" data-sku="${escapeHtml(row.sku)}">+</button>
                  </div>
                </div>
                <div class="cart-item-price-col">
                  <div class="cart-item-price">${formatPrice(row.sell_price * row.qty)}</div>
                  <button type="button" class="cart-item-remove" data-remove="${escapeHtml(row.sku)}" data-cart-remove="${escapeHtml(row.sku)}" title="Remove item">✕</button>
                </div>
              </div>
            `;
          })
          .join("")}
      </div>
    `;

    const summaryHtml = `
      <div class="cart-summary-section">
        <div class="cart-summary-row">
          <span>Subtotal</span>
          <span>${formatPrice(subtotal)}</span>
        </div>
        <div class="cart-total-row">
          <span>Total</span>
          <span class="cart-total-val">${formatPrice(total)}</span>
        </div>
      </div>
    `;

    // Checkout contact field logic (As user requested:
    // "or agr client sign in me ho to mail ya phone number option na ay sign out stage me ho to ay")
    let contactSectionHtml = "";
    if (isSigned) {
      contactSectionHtml = `
        <div class="cart-signed-badge">
          <span class="cart-signed-dot">✓</span>
          <div class="cart-signed-info">
            <div class="cart-signed-title">Ordering as: <strong>${escapeHtml(state.user.name || state.user.email)}</strong></div>
            <div class="cart-signed-subtitle">Order & Instant Delivery will be credited directly to your account.</div>
          </div>
        </div>
      `;
    } else {
      contactSectionHtml = `
        <div class="cart-auth-banner">
          <span>⚡ Have an account?</span>
          <a href="#/login" id="cart-go-signin">Sign In / Register &gt;</a>
        </div>
        <div class="cart-field-group">
          <label class="cart-field-label" for="cart-contact-input">Email or phone (where we send your order)</label>
          <input type="text" id="cart-contact-input" class="cart-input-field" placeholder="you@example.com or 03001234567" autocomplete="email tel">
          <div id="cart-contact-error" class="cart-error-msg" hidden>Please enter an email address or phone number above.</div>
        </div>
      `;
    }

    // Payment methods: dynamically populated from state.methods (admin panel configured)
    const methods = (state.methods && state.methods.length) ? state.methods : [
      { code: "binance_pay", name: "Binance Pay", network: "Direct" },
      { code: "usdt_bep20", name: "USDT (BEP-20)", network: "BSC" },
      { code: "easypaisa", name: "EasyPaisa", network: "PKR" },
    ];

    const methodsOptionsHtml = methods.map((m, idx) => `
      <option value="${escapeHtml(m.code)}" ${idx === 0 ? 'selected' : ''}>
        ${escapeHtml(m.name)}${m.network ? ` (${escapeHtml(m.network)})` : ''}
      </option>
    `).join("");

    const checkoutSectionHtml = `
      <div class="cart-checkout-section">
        ${contactSectionHtml}
        <div class="cart-field-group">
          <label class="cart-field-label" for="cart-method-select">Pay with</label>
          <select id="cart-method-select" class="cart-select-field">
            ${methodsOptionsHtml}
          </select>
        </div>
        <button type="button" class="cart-btn-primary" id="btn-cart-submit" title="Direct checkout" data-action="#/checkout">
          <span>Continue to payment ➔</span>
        </button>
        <a class="cart-btn-secondary" id="btn-cart-wa" target="_blank" rel="noopener" href="${waHref()}">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor"><path d="M12.04 2C6.58 2 2.13 6.45 2.13 11.91c0 1.97.58 3.86 1.69 5.48L2.08 22l4.76-1.69c1.55.97 3.35 1.5 5.2 1.5 5.46 0 9.91-4.45 9.91-9.91S17.5 2 12.04 2Z"/></svg>
          <span>Checkout via WhatsApp</span>
        </a>
      </div>
    `;

    els.cartBody.innerHTML = itemsHtml + summaryHtml + checkoutSectionHtml;

    const signinLink = document.getElementById("cart-go-signin");
    if (signinLink) {
      signinLink.onclick = (e) => {
        e.preventDefault();
        closeSheet(els.cartSheet);
        location.hash = "#/login";
        renderAuth("login");
      };
    }

    // Delta buttons
    els.cartBody.querySelectorAll("[data-cart-delta]").forEach((btn) => {
      btn.onclick = () => {
        const sku = btn.dataset.sku;
        const delta = Number(btn.dataset.cartDelta || 0);
        const row = state.cart.find((r) => r.sku === sku);
        if (!row) return;
        row.qty += delta;
        if (row.qty <= 0) {
          state.cart = state.cart.filter((r) => r.sku !== sku);
        }
        saveCart();
        renderCartSheet();
      };
    });

    // Remove button
    els.cartBody.querySelectorAll("[data-cart-remove]").forEach((btn) => {
      btn.onclick = () => {
        const sku = btn.dataset.cartRemove;
        removeFromCart(sku);
        renderCartSheet();
      };
    });

    // Submit handler
    const submitBtn = document.getElementById("btn-cart-submit");
    if (submitBtn) {
      submitBtn.onclick = async () => {
        let contactVal = "";
        if (isSigned) {
          contactVal = state.user.email;
        } else {
          const input = document.getElementById("cart-contact-input");
          const errEl = document.getElementById("cart-contact-error");
          contactVal = (input && input.value || "").trim();
          if (!contactVal) {
            if (errEl) errEl.hidden = false;
            if (input) input.focus();
            return;
          }
          if (errEl) errEl.hidden = true;
        }

        const methodSelect = document.getElementById("cart-method-select");
        const selectedMethod = methodSelect ? methodSelect.value : "";
        if (!selectedMethod) {
          alert("Please choose a payment method.");
          return;
        }

        submitBtn.disabled = true;
        submitBtn.innerHTML = `<span>Processing order…</span>`;

        try {
          const data = await postJSON("/api/web/checkout", {
            email: contactVal,
            payment_method: selectedMethod,
            items: state.cart.map((r) => ({ sku: r.sku, qty: r.qty })),
          });
          if (data.user) saveUser(data.user);
          state.cart = [];
          saveCart();
          state.order = data;
          closeSheet(els.cartSheet);

          if (isSigned) {
            // Signed in user: direct to Customer Dashboard delivery/orders
            window.location.href = "/account#orders";
          } else {
            // Guest: direct to Order completed screen with credentials
            location.hash = `#/order/${encodeURIComponent(data.order_code)}`;
            renderOrder(data);
          }
        } catch (err) {
          submitBtn.disabled = false;
          submitBtn.innerHTML = `<span>Continue to payment ➔</span>`;
          alert(err.message || "Failed to process checkout.");
        }
      };
    }

    openSheet(els.cartSheet);
  }

  function showView(id) {
    ["view-home", "view-collection", "view-auth", "view-checkout", "view-order"].forEach((key) => {
      const node = document.getElementById(key);
      if (node) node.hidden = key !== id;
    });
  }

  function renderCollection(kind) {
    const badgeEl = document.getElementById("collection-badge");
    const titleEl = document.getElementById("collection-title");
    const subEl = document.getElementById("collection-sub");

    if (kind === "hot-deals" || kind === "deals") {
      if (badgeEl) badgeEl.textContent = "🔥 EXCLUSIVE SALES & FLASH DEALS";
      if (titleEl) titleEl.textContent = "Hot Deals & Special Sales";
      if (subEl) subEl.textContent = "Limited-time discounts, promotional sales, and exclusive price cuts on verified accounts.";
      let items = filteredProducts("hot-deals");
      if (!items.length) {
        items = (state.featured && state.featured.hot && state.featured.hot.length)
          ? state.featured.hot
          : state.products.slice(0, 12);
      }
      els.collectionGrid.innerHTML = productCards(items);
    } else if (kind === "live-stock" || kind === "stock") {
      if (badgeEl) badgeEl.textContent = "⚡ INSTANT 1-CLICK DISPATCH";
      if (titleEl) titleEl.textContent = "Live Stock In-Stock Products";
      if (subEl) subEl.textContent = "All currently in-stock digital licenses available for immediate instant delivery.";
      let items = filteredProducts("live-stock");
      if (!items.length) {
        items = state.products.filter((p) => p.in_stock);
      }
      els.collectionGrid.innerHTML = productCards(items);
    } else if (kind === "products" || kind === "all-products" || kind === "catalog") {
      if (badgeEl) badgeEl.textContent = "📦 ALL PRODUCTS CATALOG";
      if (titleEl) titleEl.textContent = "Complete Products Directory";
      if (subEl) subEl.textContent = "All products, streaming subscriptions, AI tools, and licenses (both in-stock & pre-order).";
      const items = filteredProducts("products");
      els.collectionGrid.innerHTML = productCards(items.length ? items : state.products);
    } else if (kind === "freebies") {
      if (badgeEl) badgeEl.textContent = "🎁 COMMUNITY DROPS";
      if (titleEl) titleEl.textContent = "Freebies & Promo Drops";
      if (subEl) subEl.textContent = "Free tools, giveaway accounts, and starter access from the live SMF SHOP catalog.";
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
      if (badgeEl) badgeEl.textContent = "💎 PREMIUM SUBSCRIPTIONS";
      if (titleEl) titleEl.textContent = "Active Subscription Plans";
      if (subEl) subEl.textContent = "Paid plans, streaming accounts, and AI tool licenses with instant auto-delivery.";
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

  function renderAuth(forcedMode) {
    const signed = Boolean(state.user && state.user.email);
    const hash = window.location.hash || "";
    const mode = forcedMode || (hash.includes("/login") || state.route === "/login" ? "login" : "signup");
    const isLogin = mode === "login";

    const titleEl = document.getElementById("auth-title");
    const subEl = document.getElementById("auth-subtitle");
    const switchPrompt = document.getElementById("auth-switch-prompt");
    const switchBtn = document.getElementById("btn-switch-mode");
    const loginForm = document.getElementById("login-form");
    const signupForm = document.getElementById("signup-form");
    const signedActions = document.getElementById("auth-signed-actions");
    const tabs = document.querySelector(".auth-method-tabs");

    if (signed) {
      if (titleEl) titleEl.textContent = `Hi, ${state.user.name || "there"}`;
      if (subEl) subEl.textContent = "You are signed in to your SMF SHOP customer account.";
      if (loginForm) loginForm.hidden = true;
      if (signupForm) signupForm.hidden = true;
      if (tabs) tabs.hidden = true;
      if (switchPrompt) switchPrompt.hidden = true;
      if (switchBtn) switchBtn.hidden = true;
      if (signedActions) signedActions.hidden = false;
    } else {
      if (signedActions) signedActions.hidden = true;
      if (tabs) tabs.hidden = false;
      if (switchPrompt) switchPrompt.hidden = false;
      if (switchBtn) switchBtn.hidden = false;

      if (isLogin) {
        if (titleEl) titleEl.textContent = "Sign In";
        if (subEl) subEl.textContent = "Access your granted accounts, orders, tool modules, and wallet";
        if (loginForm) loginForm.hidden = false;
        if (signupForm) signupForm.hidden = true;
        if (switchPrompt) switchPrompt.textContent = "Don't have an account?";
        if (switchBtn) switchBtn.textContent = "Create Account ➔";
      } else {
        if (titleEl) titleEl.textContent = "Create Account";
        if (subEl) subEl.textContent = "Sign up with email or phone to access digital licenses and orders";
        if (loginForm) loginForm.hidden = true;
        if (signupForm) signupForm.hidden = false;
        if (switchPrompt) switchPrompt.textContent = "Already have an account?";
        if (switchBtn) switchBtn.textContent = "Sign In ➔";
      }
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

    const isCompleted = payload.is_completed || (first.status === "completed" || first.status === "delivered");
    const deliveredInfo = payload.delivered_info || first.delivered_info;
    const contact = payload.customer_contact || payload.customer_email || (state.user && state.user.email);

    document.getElementById("order-title").textContent = `Order #${first.order_code}`;

    let deliveryCardHtml = "";
    if (isCompleted && deliveredInfo) {
      deliveryCardHtml = `
        <div class="delivery-credentials-card">
          <div class="delivery-credentials-header">
            <span class="delivery-badge-success">⚡ INSTANT DELIVERY COMPLETE</span>
            <span style="font-size: 12px; color: #10b981; font-weight: 750;">✓ Ready to Use</span>
          </div>
          <h3 style="margin: 6px 0; font-size: 16px; color: #fff;">Your Account Credentials & Access Keys</h3>
          <p style="font-size: 13px; color: var(--muted); margin: 0 0 10px 0;">Use the credentials below to log into your account:</p>
          <div class="delivery-code-box">
            <pre id="delivery-creds-pre">${escapeHtml(deliveredInfo)}</pre>
          </div>
          <button type="button" class="btn-copy-creds" id="btn-copy-delivery-creds">
            <span>📋 Copy Credentials</span>
          </button>
          ${contact ? `<div style="margin-top: 14px; font-size: 12px; color: #94a3b8;">📧 Order confirmation & delivery copy recorded for: <strong>${escapeHtml(contact)}</strong></div>` : ""}
        </div>
      `;
    } else if (first.status === "pending") {
      deliveryCardHtml = `
        <div style="background: rgba(139, 92, 246, 0.12); border: 1px solid rgba(139, 92, 246, 0.3); border-radius: 16px; padding: 18px; margin: 18px 0; text-align: left;">
          <div style="display: flex; align-items: center; gap: 8px; font-weight: 800; font-size: 13.5px; color: #c4b5fd; margin-bottom: 6px;">
            <span style="font-size: 16px;">⏳</span> Payment Verification in Progress
          </div>
          <p style="font-size: 13px; color: var(--muted); margin: 0 0 12px 0;">Send payment to the details below. Your instant credentials will appear here automatically upon confirmation.</p>
          ${contact ? `<div style="font-size: 12px; color: #94a3b8; margin-bottom: 12px;">Contact: <strong>${escapeHtml(contact)}</strong></div>` : ""}
          <button type="button" class="cart-btn-primary" id="btn-refresh-order" style="height: 38px; font-size: 13px; max-width: 230px;">🔄 Refresh / Check Status</button>
        </div>
      `;
    }

    document.getElementById("order-body").innerHTML = `
      ${deliveryCardHtml}
      <div style="background: rgba(255, 255, 255, 0.03); border: 1px solid rgba(255, 255, 255, 0.08); border-radius: 16px; padding: 18px; margin-bottom: 20px; text-align: left;">
        <h4 style="margin: 0 0 12px 0; font-size: 14px; text-transform: uppercase; letter-spacing: 0.05em; color: var(--muted);">Order Summary</h4>
        ${(payload.orders || [first])
          .map((row) => `<div class="cart-row" style="border-bottom: 1px solid rgba(255,255,255,0.06); padding: 10px 0;"><div><strong>${escapeHtml(row.name || row.product || "")}</strong><div class="muted">${row.qty} × ${escapeHtml(row.sku || "")}</div></div><strong>${formatPrice(row.amount)}</strong></div>`)
          .join("")}
        <div style="display: flex; justify-content: space-between; align-items: center; margin-top: 12px; font-size: 16px; font-weight: 850;">
          <span>Total</span>
          <span style="color: #a78bfa; font-size: 18px;">${formatPrice(payload.total || first.amount)}</span>
        </div>
      </div>
      ${pay.address ? `
        <div style="background: rgba(14, 9, 30, 0.9); border: 1px solid rgba(139, 92, 246, 0.35); border-radius: 16px; padding: 18px; margin-bottom: 20px; text-align: left;">
          <h4 style="margin: 0 0 8px 0; font-size: 14px; color: #fff;">Payment Details: ${escapeHtml(pay.name || first.payment_method || "")}</h4>
          <p style="margin: 0 0 8px 0; font-size: 13px; color: var(--muted);">Send payment to:</p>
          <div style="background: #080612; border: 1px solid rgba(255,255,255,0.1); border-radius: 10px; padding: 10px; word-break: break-all; font-family: monospace; font-size: 13.5px; color: #00f2fe; margin-bottom: 8px;">
            ${escapeHtml(pay.address)}
          </div>
          ${pay.instructions ? `<p style="font-size: 12.5px; color: var(--muted); margin: 0;">${escapeHtml(pay.instructions)}</p>` : ""}
        </div>
      ` : ""}
      <div class="hero-actions" style="justify-content: center; gap: 12px;">
        <a class="btn btn-whatsapp" target="_blank" rel="noopener" href="${waHref()}">Confirm on WhatsApp</a>
        <a class="btn btn-primary" href="#/">Back to Catalog</a>
      </div>
    `;

    const copyBtn = document.getElementById("btn-copy-delivery-creds");
    if (copyBtn && deliveredInfo) {
      copyBtn.onclick = () => {
        navigator.clipboard.writeText(deliveredInfo).then(() => {
          copyBtn.innerHTML = `<span>✓ Copied to Clipboard!</span>`;
          setTimeout(() => {
            copyBtn.innerHTML = `<span>📋 Copy Credentials</span>`;
          }, 2000);
        });
      };
    }

    const refreshBtn = document.getElementById("btn-refresh-order");
    if (refreshBtn) {
      refreshBtn.onclick = () => {
        refreshBtn.textContent = "Checking…";
        getJSON(`/api/web/orders/${encodeURIComponent(first.order_code)}`)
          .then((data) => {
            state.order = data;
            renderOrder(data);
          })
          .catch((err) => {
            refreshBtn.textContent = "Error: " + err.message;
          });
      };
    }

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
    if (state.route === "/hot-deals" || state.route === "/deals") renderCollection("hot-deals");
    else if (state.route === "/live-stock" || state.route === "/stock") renderCollection("live-stock");
    else if (state.route === "/products" || state.route === "/all-products") renderCollection("products");
    else if (state.route === "/subscription" || state.route === "/subscriptions") renderCollection("subscription");
    else if (state.route === "/freebies") renderCollection("freebies");
    else if (state.route === "/login") renderAuth("login");
    else if (state.route === "/signup") renderAuth("signup");
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
      event.preventDefault();
      state.currency = currencyPick.dataset.currency;
      localStorage.setItem("smf_currency", state.currency);
      closeMenus();
      renderMenus();
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
      event.preventDefault();
      event.stopPropagation();
      const open = els.currencyMenu && !els.currencyMenu.hidden;
      closeMenus();
      if (els.currencyMenu) {
        els.currencyMenu.hidden = open;
      }
      if (els.currencyBtn) {
        els.currencyBtn.setAttribute("aria-expanded", String(!open));
      }
      return;
    }
    if (event.target.closest("#language-btn")) {
      const open = els.languageMenu.hidden;
      closeMenus();
      els.languageMenu.hidden = !open;
      els.languageBtn.setAttribute("aria-expanded", String(open));
      return;
    }
    if (event.target.closest("#btn-account")) {
      const signed = Boolean(state.user && state.user.email);
      if (!signed) {
        event.preventDefault();
        event.stopPropagation();
        const authDd = document.getElementById("meetway-auth-dropdown");
        const open = authDd && !authDd.hidden;
        closeMenus();
        if (authDd) authDd.hidden = open;
        return;
      }
    }
    if (event.target.closest("#meetway-auth-dropdown")) {
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
      if (state.route !== "/") {
        location.hash = "#/";
      }
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
    if (event.target.classList && event.target.classList.contains("sheet")) {
      closeSheet(event.target);
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

  // Live Restock & New Product Toast Notifications (Meetway Style, situated above support button)
  let restockTimer = null;
  let restockDismissTimer = null;
  let restockIndex = 0;

  function initRestockNotifications() {
    const card = document.getElementById("restock-notify-card");
    const tag = document.getElementById("restock-card-tag-text");
    const title = document.getElementById("restock-card-title");
    const added = document.getElementById("restock-card-added");
    const ready = document.getElementById("restock-card-stock");
    const price = document.getElementById("restock-card-price");
    const link = document.getElementById("restock-card-link");
    const btnClose = document.getElementById("btn-close-restock");

    if (!card || !title) return;

    if (btnClose) {
      btnClose.onclick = (e) => {
        e.stopPropagation();
        dismissRestockNotification();
      };
    }

    function dismissRestockNotification() {
      if (!card || card.hidden) return;
      card.classList.add("dismissing");
      clearTimeout(restockDismissTimer);
      restockDismissTimer = setTimeout(() => {
        card.hidden = true;
        card.classList.remove("dismissing");
      }, 250);
    }

    function showNextNotification() {
      const items = (state.products && state.products.length) ? state.products : (state.featured.live || []);
      if (!items || !items.length) return;

      const p = items[restockIndex % items.length];
      restockIndex++;

      const isRestock = (restockIndex % 2 === 1);
      if (isRestock) {
        card.classList.remove("new-product-mode");
        if (tag) tag.textContent = "⚡ STOCK ADDED";
        const newAdded = Math.floor(Math.random() * 8) + 2;
        if (added) added.textContent = `💥 ${newAdded} new added`;
        const readyStock = p.stock != null ? p.stock : (newAdded + 14);
        if (ready) ready.textContent = `📦 ${readyStock} ready now`;
      } else {
        card.classList.add("new-product-mode");
        if (tag) tag.textContent = "✨ PRODUCT ADDED";
        if (added) added.textContent = "⚡ Just Arrived";
        const avail = p.stock != null ? p.stock : 25;
        if (ready) ready.textContent = `📦 ${avail} in stock`;
      }

      title.textContent = p.name;
      if (price) price.textContent = formatPrice(p.sell_price);

      if (link) {
        link.onclick = (e) => {
          e.preventDefault();
          dismissRestockNotification();
          addToCart(p);
          renderCartSheet();
        };
      }

      card.classList.remove("dismissing");
      card.hidden = false;

      clearTimeout(restockDismissTimer);
      restockDismissTimer = setTimeout(() => {
        dismissRestockNotification();
      }, 6500);
    }

    clearTimeout(restockTimer);
    restockTimer = setTimeout(() => {
      showNextNotification();
      setInterval(showNextNotification, 16000);
    }, 3500);
  }

  // Approved Live Recent Order Toast System (Exact User Approved Template)
  const SAMPLE_RECENT_ORDERS = [
    { name: "Salman", phone: "03***", item: "Canva Pro 1 Year", method: "Binance", mins: 2 },
    { name: "Hamza", phone: "03***", item: "ChatGPT Plus 1 Month", method: "JazzCash", mins: 4 },
    { name: "Ayesha", phone: "03***", item: "Netflix 4K Ultra HD", method: "EasyPaisa", mins: 1 },
    { name: "Usman", phone: "03***", item: "CapCut Pro 1 Year", method: "Binance", mins: 6 },
    { name: "Fatima", phone: "03***", item: "Spotify Premium 6 Months", method: "Nayapay", mins: 3 },
    { name: "Ali", phone: "03***", item: "YouTube Premium 1 Year", method: "Sadapay", mins: 5 },
    { name: "Zainab", phone: "03***", item: "Adobe Creative Cloud", method: "Bank Transfer", mins: 8 },
    { name: "Bilal", phone: "03***", item: "Telegram Premium 1 Year", method: "Binance", mins: 2 }
  ];

  let recentOrderIndex = 0;
  let recentOrderDismissTimer = null;

  function showRecentOrderToast(order) {
    const container = document.getElementById("recent-order-toast");
    if (!container) return;

    const name = escapeHtml(order.name || "Customer");
    const mask = escapeHtml(order.phone || "03***");
    const item = escapeHtml(order.item || "Canva Pro 1 Year");
    const method = escapeHtml(order.method || "Binance");
    const timeText = order.mins ? `${order.mins} mins ago` : "just now";

    container.innerHTML = `
      <div class="recent-order-line1">
        🛍️ <span class="order-toast-name">${name}</span> <span class="order-toast-mask">(${mask})</span> just purchased <span class="order-toast-item">${item}</span> Through <span class="order-toast-method">${method}</span>
      </div>
      <div class="recent-order-line2">
        <span class="order-toast-pulse"></span>
        <span>⚡ Verified Order · ${timeText}</span>
      </div>
    `;

    container.hidden = false;
    container.classList.remove("dismissing");

    clearTimeout(recentOrderDismissTimer);
    recentOrderDismissTimer = setTimeout(() => {
      container.classList.add("dismissing");
      setTimeout(() => {
        container.hidden = true;
        container.classList.remove("dismissing");
      }, 300);
    }, 6000);
  }

  function initRecentOrdersToast() {
    setTimeout(() => {
      showRecentOrderToast(SAMPLE_RECENT_ORDERS[recentOrderIndex % SAMPLE_RECENT_ORDERS.length]);
      recentOrderIndex++;
      setInterval(() => {
        showRecentOrderToast(SAMPLE_RECENT_ORDERS[recentOrderIndex % SAMPLE_RECENT_ORDERS.length]);
        recentOrderIndex++;
      }, 16000);
    }, 4500);
  }

  window.triggerLiveOrderToast = (customerName, phone, productName, paymentMethod) => {
    const firstName = (customerName || "Customer").trim().split(" ")[0];
    let phoneMask = "03***";
    if (phone && phone.length >= 4) {
      phoneMask = phone.substring(0, 3) + "***";
    }
    showRecentOrderToast({
      name: firstName,
      phone: phoneMask,
      item: productName || "Canva Pro 1 Year",
      method: paymentMethod || "Binance",
      mins: 0
    });
  };

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
      location.hash = "#/products";
      renderCollection("products");
      window.scrollTo({ top: 0, behavior: "smooth" });
    });
  }

  const btnPopLogin = document.getElementById("btn-pop-login");
  if (btnPopLogin) {
    btnPopLogin.addEventListener("click", (e) => {
      const authDd = document.getElementById("meetway-auth-dropdown");
      if (authDd) authDd.hidden = true;
      location.hash = "#/login";
      renderAuth("login");
    });
  }

  const btnPopRegister = document.getElementById("btn-pop-register");
  if (btnPopRegister) {
    btnPopRegister.addEventListener("click", (e) => {
      const authDd = document.getElementById("meetway-auth-dropdown");
      if (authDd) authDd.hidden = true;
      location.hash = "#/signup";
      renderAuth("signup");
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

  // Switch between Login and Signup modes
  const btnSwitchMode = document.getElementById("btn-switch-mode");
  if (btnSwitchMode) {
    btnSwitchMode.addEventListener("click", () => {
      const loginForm = document.getElementById("login-form");
      const isLoginVisible = loginForm && !loginForm.hidden;
      if (isLoginVisible) {
        location.hash = "#/signup";
        renderAuth("signup");
      } else {
        location.hash = "#/login";
        renderAuth("login");
      }
    });
  }

  // Email vs Phone Tab Switcher
  const tabAuthEmail = document.getElementById("tab-auth-email");
  const tabAuthPhone = document.getElementById("tab-auth-phone");
  const labelLoginIdent = document.getElementById("label-login-identifier");
  const loginInputIdent = document.getElementById("login-input-ident");
  const labelSignupIdent = document.getElementById("label-signup-identifier");
  const signupInputIdent = document.getElementById("signup-input-ident");
  const iconLoginIdent = document.getElementById("icon-login-ident");
  const iconSignupIdent = document.getElementById("icon-signup-ident");

  const emailSvg = `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="m4 4 16 0c1.1 0 2 .9 2 2l0 12c0 1.1-.9 2-2 2l-16 0c-1.1 0-2-.9-2-2l0-12c0-1.1.9-2 2-2z"/><polyline points="22,6 12,13 2,6"/></svg>`;
  const phoneSvg = `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect width="14" height="20" x="5" y="2" rx="2" ry="2"/><path d="M12 18h.01"/></svg>`;

  if (tabAuthEmail) {
    tabAuthEmail.addEventListener("click", () => {
      tabAuthEmail.classList.add("active");
      tabAuthEmail.setAttribute("aria-selected", "true");
      if (tabAuthPhone) {
        tabAuthPhone.classList.remove("active");
        tabAuthPhone.setAttribute("aria-selected", "false");
      }
      if (labelLoginIdent) labelLoginIdent.textContent = "EMAIL ADDRESS OR USERNAME";
      if (loginInputIdent) {
        loginInputIdent.placeholder = "you@email.com";
        loginInputIdent.type = "text";
      }
      if (labelSignupIdent) labelSignupIdent.textContent = "EMAIL ADDRESS";
      if (signupInputIdent) {
        signupInputIdent.placeholder = "you@email.com";
        signupInputIdent.type = "text";
      }
      if (iconLoginIdent) iconLoginIdent.innerHTML = emailSvg;
      if (iconSignupIdent) iconSignupIdent.innerHTML = emailSvg;
    });
  }

  if (tabAuthPhone) {
    tabAuthPhone.addEventListener("click", () => {
      tabAuthPhone.classList.add("active");
      tabAuthPhone.setAttribute("aria-selected", "true");
      if (tabAuthEmail) {
        tabAuthEmail.classList.remove("active");
        tabAuthEmail.setAttribute("aria-selected", "false");
      }
      if (labelLoginIdent) labelLoginIdent.textContent = "PHONE NUMBER";
      if (loginInputIdent) {
        loginInputIdent.placeholder = "+92 300 1234567 or 0300...";
        loginInputIdent.type = "tel";
      }
      if (labelSignupIdent) labelSignupIdent.textContent = "PHONE NUMBER";
      if (signupInputIdent) {
        signupInputIdent.placeholder = "+92 300 1234567 or 0300...";
        signupInputIdent.type = "tel";
      }
      if (iconLoginIdent) iconLoginIdent.innerHTML = phoneSvg;
      if (iconSignupIdent) iconSignupIdent.innerHTML = phoneSvg;
    });
  }

  // Password Visibility Eye Toggles
  document.querySelectorAll(".btn-toggle-eye").forEach((btn) => {
    btn.addEventListener("click", () => {
      const targetId = btn.dataset.target;
      const input = document.getElementById(targetId);
      if (!input) return;
      if (input.type === "password") {
        input.type = "text";
        btn.innerHTML = `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="m9.88 9.88a3 3 0 1 0 4.24 4.24"/><path d="M10.73 5.08A10.43 10.43 0 0 1 12 5c7 0 10 7 10 7a13.16 13.16 0 0 1-1.67 2.68"/><path d="M6.61 6.61A13.526 13.526 0 0 0 2 12s3 7 10 7a9.74 9.74 0 0 0 5.39-1.61"/><line x1="2" x2="22" y1="2" y2="22"/></svg>`;
      } else {
        input.type = "password";
        btn.innerHTML = `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M2 12s3-7 10-7 10 7 10 7-3 7-10 7-10-7-10-7Z"/><circle cx="12" cy="12" r="3"/></svg>`;
      }
    });
  });

  const showLoginBtn = document.getElementById("show-login");
  if (showLoginBtn) {
    showLoginBtn.onclick = () => {
      location.hash = "#/login";
      renderAuth("login");
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
      renderAuth("signup");
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
      const clientName = (data.user && data.user.name) || document.getElementById("checkout-name").value || "Customer";
      const clientPhone = document.getElementById("checkout-email").value || "";
      const firstItem = (data.items && data.items[0] && data.items[0].name) || "License";
      window.triggerLiveOrderToast(clientName, clientPhone, firstItem, method || "Binance");
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
    initRestockNotifications();
    initRecentOrdersToast();
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
      initRestockNotifications();
      initRecentOrdersToast();
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
