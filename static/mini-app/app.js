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

  const state = {
    shop: null,
    products: [],
    featured: { live: [], hot: [], best_seller: [] },
    categories: [],
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
      els.eyebrow.textContent = state.shop.eyebrow;
      els.headline.textContent = state.shop.headline;
      els.tagline.textContent = state.shop.tagline;
      [els.whatsapp, els.whatsappCatalog, els.whatsappCheckout].forEach((node) => {
        if (!node) return;
        node.href = waHref();
        node.hidden = false;
        node.style.display = "";
      });
    }
    const signed = Boolean(state.user && state.user.email);
    const signBtn = document.getElementById("btn-signin");
    signBtn.textContent = signed ? state.user.name || "Account" : "Sign up";
    signBtn.href = "#/signup";
    document.getElementById("btn-account").href = "#/signup";
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

  function productCards(rows) {
    if (!rows.length) return `<p class="empty">Nothing here yet.</p>`;
    return rows
      .map((product, index) => {
        const accent = ACCENTS[index % ACCENTS.length];
        const discount = discountPercent(product);
        const noteOpen = Boolean(state.notes[product.sku]);
        const sale = discount ? `<div class="card-badges"><span class="tag hot">Sale −${discount}%</span></div>` : "";
        const logo = product.image_url
          ? `<img class="product-logo" src="${escapeHtml(product.image_url)}" alt="">`
          : `<div class="product-emoji" aria-hidden>${escapeHtml(product.emoji || "🛍️")}</div>`;
        const warrantyBlock = product.warranty_label
          ? `<div class="warranty-row"><span aria-hidden>🛡️</span> ${escapeHtml(product.warranty_label)}</div>`
          : "";
        const noteBtn = (product.description || product.note)
          ? `<button type="button" class="view-note-btn" data-detail="${escapeHtml(product.sku)}"><span aria-hidden>📋</span> VIEW DESCRIPTION</button>`
          : "";

        const popoverFields = [];
        popoverFields.push('<div class="popover-row"><span class="popover-label">Price</span><span class="popover-val">' + usdPrice(product.sell_price) + '</span></div>');
        if (product.min_qty || product.max_qty) {
          const min = product.min_qty || 1;
          const max = product.max_qty || 1;
          const qLimit = (min === max && min === 1) ? "1" : (min + " to " + max);
          popoverFields.push('<div class="popover-row"><span class="popover-label">Quantity</span><span class="popover-val">' + qLimit + '</span></div>');
        }
        if (product.delivery_type) {
          popoverFields.push('<div class="popover-row"><span class="popover-label">Delivery</span><span class="popover-val">' + escapeHtml(product.delivery_type) + '</span></div>');
        }
        if (product.warranty_label) {
          popoverFields.push('<div class="popover-row"><span class="popover-label">Warranty</span><span class="popover-val">' + escapeHtml(product.warranty_label) + '</span></div>');
        }
        popoverFields.push('<div class="popover-row"><span class="popover-label">Availability</span><span class="popover-val">' + escapeHtml(product.stock_label) + (product.stock != null ? ' (' + product.stock + ')' : '') + '</span></div>');

        const popoverHtml = '<div class="quick-details-popover" id="popover-' + escapeHtml(product.sku) + '" hidden><div class="popover-title">Product Details</div><div class="popover-table">' + popoverFields.join('') + '</div></div>';

        return `
          <article class="product-card accent-${accent}">
            ${sale}
            <div class="card-top-row">
              <div style="position: relative; display: inline-block;">
                <button type="button" class="card-info-btn" data-info="${escapeHtml(product.sku)}" aria-label="About ${escapeHtml(product.name)}">i</button>
                ${popoverHtml}
              </div>
              ${product.delivery_type === "manual" ? "" : '<span class="instant-badge"><span aria-hidden>⚡</span> Instant</span>'}
            </div>
            <div class="product-card-top">
              ${logo}
              <div>
                <h3><a href="#product-${encodeURIComponent(product.sku)}" data-detail="${escapeHtml(product.sku)}">${escapeHtml(product.name)}</a></h3>
                <div class="item-meta">${escapeHtml(product.category || "General")}</div>
              </div>
            </div>
            ${warrantyBlock}
            ${noteBtn}
            <div class="card-meta-row">
              <div class="price-block">
                <span class="price-only">Only</span>
                <span class="price-value">${usdPrice(product.sell_price)}</span>
                ${discount ? `<div class="old">${usdPrice(product.original_price)}</div>` : ""}
                <span class="price-pkr">${pkrPrice(product.sell_price)}</span>
              </div>
              <div class="stock-block" title="${escapeHtml(product.stock_label)}">
                <span class="stock-icon" aria-hidden>📦</span>
                <span class="stock-label">Stock</span>
                <span class="stock-number${product.in_stock ? "" : " out"}">${product.stock != null ? escapeHtml(product.stock) : "—"}</span>
              </div>
            </div>
            <button type="button" class="btn btn-add-cart" data-add="${escapeHtml(product.sku)}" ${product.in_stock ? "" : "disabled"}>
              <span aria-hidden>🛒</span> Add to Cart
            </button>
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
    els.productBody.innerHTML = `
      <div class="item-icon" style="width:100%;height:140px;font-size:40px;object-fit:cover;border-radius:12px;display:grid;place-items:center;background:#1c1730;overflow:hidden;margin-bottom:16px;">${
        product.image_url ? `<img src="${escapeHtml(product.image_url)}" style="width:100%;height:100%;object-fit:cover;" alt="">` : product.emoji || "🛍️"
      }</div>
      <h2 style="margin-top:16px;">${escapeHtml(product.name)}</h2>
      <h3 style="margin-top:20px;font-size:14px;color:var(--muted);text-transform:uppercase;letter-spacing:0.05em;">BEFORE YOU ORDER</h3>
      <div class="product-description-text" style="white-space: pre-wrap; margin-top:8px; line-height: 1.5;">${escapeHtml(product.description || product.note || "")}</div>
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
    const title = kind === "freebies" ? "Freebies" : "Subscription";
    document.getElementById("collection-eyebrow").textContent = "SMF SHOP";
    document.getElementById("collection-title").textContent = title;
    document.getElementById("collection-sub").textContent =
      kind === "freebies" ? "Free tools and starter access from the live catalog." : "Paid plans and premium accounts.";
    els.collectionGrid.innerHTML = productCards(filteredProducts(kind));
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
    const logout = document.getElementById("btn-logout");
    if (logout) logout.hidden = !signed;
    const lede = els.viewAuth.querySelector(".lede");
    if (lede) {
      lede.textContent = signed
        ? "You are signed in with email — not Telegram."
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
    if (state.route === "/subscription") renderCollection("subscription");
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
      const popoverId = 'popover-' + infoBtn.dataset.info;
      const targetPopover = document.getElementById(popoverId);
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

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      document.querySelectorAll('.quick-details-popover').forEach(p => p.hidden = true);
    }
  });

  document.getElementById("btn-cart").onclick = () => renderCartSheet();
  document.getElementById("btn-support").onclick = () => {
    const href = waHref();
    if (href && href !== "#") window.open(href, "_blank", "noopener");
  };
  els.search.addEventListener("input", () => {
    state.query = els.search.value;
    if (state.route === "/subscription") renderCollection("subscription");
    else if (state.route === "/freebies") renderCollection("freebies");
    else renderGrid();
  });
  document.getElementById("btn-explore").addEventListener("click", (event) => {
    event.preventDefault();
    location.hash = "#/";
    setTimeout(() => document.getElementById("catalog").scrollIntoView({ behavior: "smooth" }), 50);
  });
  document.getElementById("show-login").onclick = () => {
    document.getElementById("signup-form").hidden = true;
    document.getElementById("login-form").hidden = false;
    document.getElementById("auth-title").textContent = "Log in to SMF SHOP";
  };
  document.getElementById("btn-logout").onclick = () => {
    saveUser(null);
    location.hash = "#/signup";
    renderAuth();
  };

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
      location.hash = state.cart.length ? "#/checkout" : "#/";
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
      location.hash = state.cart.length ? "#/checkout" : "#/";
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

  Promise.all([
    getJSON("/api/web/shop"),
    getJSON("/api/web/products"),
    getJSON("/api/web/featured"),
    getJSON("/api/web/categories"),
    getJSON("/api/web/payment-methods").catch(() => []),
  ])
    .then(([shop, products, featured, categories, methods]) => {
      state.shop = shop;
      state.products = products;
      state.featured = featured;
      state.categories = categories;
      state.methods = methods;
      applyRoute();
    })
    .catch((err) => {
      els.grid.innerHTML = `<p class="empty">Could not load shop (${escapeHtml(err.message)}).</p>`;
    });
})();
