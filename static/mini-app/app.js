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

  const state = {
    shop: null,
    products: [],
    featured: { live: [], hot: [], best_seller: [] },
    categories: [],
    currency: localStorage.getItem("smf_currency") || "USD",
    language: localStorage.getItem("smf_language") || "en",
    filter: "all",
    query: "",
    categoryId: null,
    cart: JSON.parse(localStorage.getItem("smf_cart") || "[]"),
  };

  const els = {
    eyebrow: document.getElementById("shop-eyebrow"),
    headline: document.getElementById("shop-headline"),
    tagline: document.getElementById("shop-tagline"),
    whatsapp: document.getElementById("btn-whatsapp"),
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
    catalogTitle: document.getElementById("catalog-title"),
    productSheet: document.getElementById("product-sheet"),
    productBody: document.getElementById("product-sheet-body"),
    cartSheet: document.getElementById("cart-overlay"),
    cartBody: document.getElementById("cart-body"),
    accountSheet: document.getElementById("account-sheet"),
    accountBody: document.getElementById("account-body"),
  };

  const copy = {
    en: {
      subscription: "Subscription",
      freebies: "Freebies",
      signIn: "Sign in",
      explore: "Explore Products",
      whatsapp: "WhatsApp order",
      catalog: "Explore products",
      catalogSub: "Same catalog and prices as the Telegram shop.",
      empty: "Nothing here yet.",
      live: "Live",
      hot: "Hot",
      best: "Best Seller",
    },
  };

  function t(key) {
    const pack = copy[state.language] || copy.en;
    return pack[key] || copy.en[key] || key;
  }

  function tgUser() {
    return (tg && tg.initDataUnsafe && tg.initDataUnsafe.user) || null;
  }

  async function getJSON(path) {
    const res = await fetch(path, { headers: { Accept: "application/json" } });
    if (!res.ok) throw new Error(`${path} failed`);
    return res.json();
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

  function saveCart() {
    localStorage.setItem("smf_cart", JSON.stringify(state.cart));
    renderCartCount();
  }

  function addToCart(product) {
    const existing = state.cart.find((row) => row.sku === product.sku);
    if (existing) existing.qty += 1;
    else state.cart.push({ sku: product.sku, name: product.name, emoji: product.emoji, sell_price: product.sell_price, qty: 1 });
    saveCart();
  }

  function waHref(product) {
    const base = (state.shop && state.shop.whatsapp_url) || "";
    if (!base) return state.shop && state.shop.support_url ? state.shop.support_url : "#";
    const text = product
      ? `Hi SMF SHOP, I want to order ${product.name} (${product.sku}) for ${formatPrice(product.sell_price)}`
      : "Hi SMF SHOP, I want to place an order from the Mini App.";
    return `${base}?text=${encodeURIComponent(text)}`;
  }

  function closeMenus() {
    document.querySelectorAll(".menu").forEach((menu) => {
      menu.hidden = true;
    });
    document.querySelectorAll(".chip").forEach((btn) => btn.setAttribute("aria-expanded", "false"));
  }

  function renderMenus() {
    const currencies = (state.shop && state.shop.currencies) || [
      { code: "USD", label: "USD ($)" },
    ];
    els.currencyMenu.innerHTML = currencies
      .map((item) => `<button type="button" data-currency="${item.code}">${item.label}</button>`)
      .join("");
    const current = currencies.find((item) => item.code === state.currency) || currencies[0];
    els.currencyBtn.textContent = current.label;

    const languages = (state.shop && state.shop.languages) || [{ code: "en", name: "English", flag: "🇬🇧" }];
    els.languageMenu.innerHTML = languages
      .map((item) => `<button type="button" data-language="${item.code}">${item.flag} ${item.name}</button>`)
      .join("");
    const lang = languages.find((item) => item.code === state.language) || languages[0];
    state.language = lang.code;
    els.languageBtn.textContent = `${lang.flag} ${lang.name}`;
  }

  function renderChrome() {
    if (!state.shop) return;
    els.eyebrow.textContent = state.shop.eyebrow;
    els.headline.textContent = state.shop.headline;
    els.tagline.textContent = state.shop.tagline;
    document.getElementById("nav-subscription").textContent = t("subscription");
    document.getElementById("nav-freebies").textContent = t("freebies");
    document.getElementById("btn-signin").textContent = t("signIn");
    document.getElementById("btn-explore").textContent = t("explore");
    els.whatsapp.textContent = t("whatsapp");
    els.whatsapp.href = waHref();
    if (!state.shop.whatsapp_url && !state.shop.support_url) {
      els.whatsapp.style.display = "none";
    }
    els.catalogTitle.textContent = t("catalog");
    document.getElementById("catalog-sub").textContent = t("catalogSub");
    renderMenus();
    renderCartCount();
  }

  function renderCartCount() {
    const count = state.cart.reduce((sum, row) => sum + row.qty, 0);
    els.cartCount.hidden = count === 0;
    els.cartCount.textContent = String(count);
  }

  function itemButton(product, tag) {
    const icon = product.image_url
      ? `<img src="${product.image_url}" alt="">`
      : (product.emoji || "🛍️");
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

  function visibleProducts() {
    const q = state.query.trim().toLowerCase();
    return state.products.filter((product) => {
      if (state.filter === "subscription" && product.is_free) return false;
      if (state.filter === "freebies" && !product.is_free) return false;
      if (state.categoryId && product.category_id !== state.categoryId) return false;
      if (!q) return true;
      const hay = `${product.name} ${product.sku} ${product.category || ""} ${product.description || ""}`.toLowerCase();
      return hay.includes(q);
    });
  }

  function renderPills() {
    const counts = {};
    state.products.forEach((product) => {
      counts[product.category_id] = (counts[product.category_id] || 0) + 1;
    });
    const pills = [
      `<button type="button" class="pill ${state.categoryId == null && state.filter === "all" ? "active" : ""}" data-cat="">All</button>`,
      ...state.categories
        .filter((cat) => counts[cat.id])
        .map(
          (cat) =>
            `<button type="button" class="pill ${state.categoryId === cat.id ? "active" : ""}" data-cat="${cat.id}">${cat.emoji || ""} ${escapeHtml(cat.name)}</button>`
        ),
    ];
    els.pills.innerHTML = pills.join("");
  }

  function renderGrid() {
    const rows = visibleProducts();
    if (!rows.length) {
      els.grid.innerHTML = `<p class="empty">${t("empty")}</p>`;
      return;
    }
    els.grid.innerHTML = rows
      .map((product) => {
        const tag = product.is_free
          ? `<span class="tag live">Free</span>`
          : product.in_stock
            ? `<span class="tag live">Live</span>`
            : `<span class="tag hot">Out</span>`;
        const old = product.original_price
          ? `<span class="old">${formatPrice(product.original_price)}</span>`
          : "";
        const icon = product.image_url
          ? `<span class="item-icon"><img src="${product.image_url}" alt=""></span>`
          : `<span class="item-icon">${product.emoji || "🛍️"}</span>`;
        return `
          <button type="button" class="card" data-sku="${product.sku}">
            <div class="card-top">${icon}${tag}</div>
            <h3>${escapeHtml(product.name)}</h3>
            <div class="item-meta">${escapeHtml(product.category || "General")} · ${escapeHtml(product.stock_label)}</div>
            <div class="price-row"><span class="price">${formatPrice(product.sell_price)}</span>${old}</div>
          </button>
        `;
      })
      .join("");
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
    const old = product.original_price ? `<span class="old">${formatPrice(product.original_price)}</span>` : "";
    els.productBody.innerHTML = `
      <div class="item-icon" style="width:56px;height:56px;font-size:26px">${
        product.image_url ? `<img src="${product.image_url}" alt="">` : product.emoji || "🛍️"
      }</div>
      <h2>${escapeHtml(product.name)}</h2>
      <p class="muted">${escapeHtml(product.category || "General")} · ${escapeHtml(product.stock_label)}</p>
      <div class="price-row"><span class="price">${formatPrice(product.sell_price)}</span>${old}</div>
      <p>${escapeHtml(product.description || product.note || "")}</p>
      <div class="hero-actions">
        <button type="button" class="btn btn-primary" id="sheet-add">Add to cart</button>
        <a class="btn btn-whatsapp" target="_blank" rel="noopener" href="${waHref(product)}">WhatsApp order</a>
      </div>
    `;
    document.getElementById("sheet-add").onclick = () => {
      addToCart(product);
      closeSheet(els.productSheet);
    };
    openSheet(els.productSheet);
  }

  function renderCartSheet() {
    if (!state.cart.length) {
      els.cartBody.innerHTML = `<p class="empty">Your cart is empty.</p>`;
      openSheet(els.cartSheet);
      return;
    }
    const total = state.cart.reduce((sum, row) => sum + row.sell_price * row.qty, 0);
    els.cartBody.innerHTML = `
      ${state.cart
        .map(
          (row) => `
        <div class="cart-row">
          <div>
            <strong>${escapeHtml(row.name)}</strong>
            <div class="muted">${formatPrice(row.sell_price)}</div>
          </div>
          <div>
            <button type="button" class="qty-btn" data-sku="${row.sku}" data-delta="-1">−</button>
            ${row.qty}
            <button type="button" class="qty-btn" data-sku="${row.sku}" data-delta="1">+</button>
          </div>
        </div>
      `
        )
        .join("")}
      <p><strong>Total ${formatPrice(total)}</strong></p>
      <div class="hero-actions">
        <a class="btn btn-whatsapp" target="_blank" rel="noopener" href="${waHref()}">WhatsApp order</a>
      </div>
    `;
    openSheet(els.cartSheet);
  }

  function renderAccount(mode) {
    const user = tgUser();
    if (user) {
      els.accountBody.innerHTML = `
        <p><strong>${escapeHtml(user.first_name || "Member")} ${escapeHtml(user.last_name || "")}</strong></p>
        <p class="muted">@${escapeHtml(user.username || "telegram")} · signed in via Telegram</p>
      `;
    } else if (mode === "signin") {
      els.accountBody.innerHTML = `
        <p>Open <strong>SMF SHOP</strong> from the Telegram bot to sign in automatically.</p>
        <p class="muted">No extra password — your Telegram account is the login.</p>
      `;
    } else {
      els.accountBody.innerHTML = `
        <p>You are browsing as a guest.</p>
        <p class="muted">Sign in from Telegram to sync your account.</p>
        <div class="hero-actions"><button type="button" class="btn btn-primary" id="sheet-signin">Sign in</button></div>
      `;
      const btn = document.getElementById("sheet-signin");
      if (btn) btn.onclick = () => renderAccount("signin");
    }
    openSheet(els.accountSheet);
  }

  function escapeHtml(value) {
    return String(value || "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function setFilter(filter) {
    state.filter = filter;
    state.categoryId = null;
    document.querySelectorAll(".nav-link[data-filter]").forEach((btn) => {
      btn.classList.toggle("active", btn.dataset.filter === filter);
    });
    renderPills();
    renderGrid();
    document.getElementById("catalog").scrollIntoView({ behavior: "smooth" });
  }

  document.addEventListener("click", (event) => {
    const nav = event.target.closest("[data-filter]");
    if (nav) setFilter(nav.dataset.filter);

    const currencyPick = event.target.closest("[data-currency]");
    if (currencyPick) {
      state.currency = currencyPick.dataset.currency;
      localStorage.setItem("smf_currency", state.currency);
      closeMenus();
      renderChrome();
      renderFeatured();
      renderGrid();
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

    const skuBtn = event.target.closest("[data-sku]:not(.qty-btn)");
    if (skuBtn && skuBtn.dataset.sku && !event.target.closest(".qty-btn")) {
      const product = productBySku(skuBtn.dataset.sku);
      if (product) renderProductSheet(product);
    }

    const qty = event.target.closest(".qty-btn");
    if (qty) {
      const row = state.cart.find((item) => item.sku === qty.dataset.sku);
      if (row) {
        row.qty += Number(qty.dataset.delta);
        if (row.qty <= 0) state.cart = state.cart.filter((item) => item.sku !== row.sku);
        saveCart();
        renderCartSheet();
      }
    }

    const cat = event.target.closest("[data-cat]");
    if (cat) {
      state.filter = "all";
      state.categoryId = cat.dataset.cat ? Number(cat.dataset.cat) : null;
      document.querySelectorAll(".nav-link[data-filter]").forEach((btn) => btn.classList.remove("active"));
      renderPills();
      renderGrid();
    }

    if (event.target.closest("[data-close]")) {
      closeSheet(event.target.closest(".sheet"));
    }

    if (
      !event.target.closest(".dropdown") &&
      !event.target.closest("#currency-btn") &&
      !event.target.closest("#language-btn")
    ) {
      closeMenus();
    }
  });

  document.getElementById("btn-cart").onclick = () => renderCartSheet();
  document.getElementById("btn-account").onclick = () => renderAccount("account");
  document.getElementById("btn-signin").onclick = () => renderAccount("signin");
  document.getElementById("btn-support").onclick = () => {
    const href = waHref() !== "#" ? waHref() : state.shop && state.shop.support_url;
    if (href && href !== "#") window.open(href, "_blank", "noopener");
  };
  els.search.addEventListener("input", () => {
    state.query = els.search.value;
    renderGrid();
  });
  document.getElementById("btn-explore").addEventListener("click", (event) => {
    event.preventDefault();
    document.getElementById("catalog").scrollIntoView({ behavior: "smooth" });
  });
  document.getElementById("brand-home").addEventListener("click", (event) => {
    event.preventDefault();
    state.filter = "all";
    state.categoryId = null;
    state.query = "";
    els.search.value = "";
    document.querySelectorAll(".nav-link[data-filter]").forEach((btn) => btn.classList.remove("active"));
    renderPills();
    renderGrid();
    window.scrollTo({ top: 0, behavior: "smooth" });
  });

  Promise.all([
    getJSON("/api/web/shop"),
    getJSON("/api/web/products"),
    getJSON("/api/web/featured"),
    getJSON("/api/web/categories"),
  ])
    .then(([shop, products, featured, categories]) => {
      state.shop = shop;
      state.products = products;
      state.featured = featured;
      state.categories = categories;
      renderChrome();
      renderFeatured();
      renderPills();
      renderGrid();
    })
    .catch((err) => {
      els.grid.innerHTML = `<p class="empty">Could not load shop (${escapeHtml(err.message)}).</p>`;
    });
})();
