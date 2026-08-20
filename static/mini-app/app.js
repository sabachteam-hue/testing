const tg = window.Telegram && window.Telegram.WebApp;
if (tg) {
  tg.ready();
  tg.expand();
  try { tg.setHeaderColor("#09051A"); } catch (err) { /* older clients */ }
  try { tg.setBackgroundColor("#09051A"); } catch (err) { /* older clients */ }
}

const state = {
  products: [],
  categories: [],
  categoryId: null,
  query: "",
  cart: JSON.parse(localStorage.getItem("smf-mini-cart") || "[]"),
};

const grid = document.getElementById("grid");
const cats = document.getElementById("cats");
const empty = document.getElementById("empty");
const search = document.getElementById("search");
const sheet = document.getElementById("sheet");
const sheetCard = document.getElementById("sheet-card");
const cartCount = document.getElementById("cart-count");
const cartBtn = document.getElementById("cart-btn");

function money(value) {
  return `$${Number(value || 0).toFixed(2)}`;
}

function saveCart() {
  localStorage.setItem("smf-mini-cart", JSON.stringify(state.cart));
  const count = state.cart.reduce((sum, item) => sum + item.qty, 0);
  cartCount.textContent = String(count);
  cartCount.classList.toggle("hidden", count === 0);
}

function addToCart(product) {
  if (!product.in_stock) return;
  const existing = state.cart.find((item) => item.sku === product.sku);
  if (existing) existing.qty += 1;
  else state.cart.push({ sku: product.sku, name: product.name, price: product.sell_price, qty: 1 });
  saveCart();
}

function filtered() {
  const q = state.query.trim().toLowerCase();
  return state.products.filter((product) => {
    if (state.categoryId != null && product.category_id !== state.categoryId) return false;
    if (q && !(product.name || "").toLowerCase().includes(q) && !(product.sku || "").toLowerCase().includes(q)) {
      return false;
    }
    return true;
  });
}

function renderCats() {
  const all = [{ id: null, name: "All", emoji: "✨" }, ...state.categories];
  cats.innerHTML = all
    .map(
      (cat) =>
        `<button class="chip${cat.id === state.categoryId ? " active" : ""}" data-id="${cat.id ?? ""}">${cat.emoji || ""} ${cat.name}</button>`,
    )
    .join("");
  cats.querySelectorAll(".chip").forEach((btn) => {
    btn.addEventListener("click", () => {
      const raw = btn.getAttribute("data-id");
      state.categoryId = raw ? Number(raw) : null;
      renderCats();
      renderGrid();
    });
  });
}

function renderGrid() {
  const rows = filtered();
  empty.classList.toggle("hidden", rows.length > 0);
  grid.innerHTML = rows
    .map((product) => {
      const sale = product.original_price && product.original_price > product.sell_price;
      return `<article class="card" data-sku="${product.sku}">
        <div class="card-top">
          <div class="emoji">${product.emoji || "🛍️"}</div>
          ${product.delivery_type === "manual" ? "" : '<span class="instant">⚡ Instant</span>'}
        </div>
        <div class="name">${product.name}</div>
        <div class="meta">${product.category || "General"} · <span class="${product.in_stock ? "stock-ok" : "stock-out"}">${product.stock_label}</span></div>
        <div class="price">${money(product.sell_price)}${sale ? `<span class="old">${money(product.original_price)}</span>` : ""}</div>
        <button class="add" ${product.in_stock ? "" : "disabled"} data-add="${product.sku}">Add to cart</button>
      </article>`;
    })
    .join("");

  grid.querySelectorAll(".card").forEach((card) => {
    card.addEventListener("click", (event) => {
      if (event.target.closest("[data-add]")) return;
      openSheet(card.getAttribute("data-sku"));
    });
  });
  grid.querySelectorAll("[data-add]").forEach((btn) => {
    btn.addEventListener("click", (event) => {
      event.stopPropagation();
      const product = state.products.find((row) => row.sku === btn.getAttribute("data-add"));
      if (product) addToCart(product);
    });
  });
}

function openSheet(sku) {
  const product = state.products.find((row) => row.sku === sku);
  if (!product) return;
  sheetCard.innerHTML = `
    <h3>${product.emoji || ""} ${product.name}</h3>
    <p class="meta">${product.category || "General"} · ${product.stock_label}</p>
    <p class="price">${money(product.sell_price)}</p>
    <p class="lede">${product.description || product.note || ""}</p>
    ${product.warranty_label ? `<p class="meta">🛡️ ${product.warranty_label}</p>` : ""}
    <div class="sheet-actions">
      <button class="ghost" id="sheet-close">Close</button>
      <button class="add" id="sheet-add" ${product.in_stock ? "" : "disabled"}>Add to cart</button>
    </div>
  `;
  sheet.classList.remove("hidden");
  document.getElementById("sheet-close").onclick = () => sheet.classList.add("hidden");
  document.getElementById("sheet-add").onclick = () => {
    addToCart(product);
    sheet.classList.add("hidden");
  };
}

sheet.addEventListener("click", (event) => {
  if (event.target === sheet) sheet.classList.add("hidden");
});

cartBtn.addEventListener("click", () => {
  if (!state.cart.length) {
    sheetCard.innerHTML = `<h3>Cart is empty</h3><p class="lede">Add a product, then complete the order in SMF SHOP with Shop All.</p><div class="sheet-actions"><button class="add" id="sheet-close">OK</button></div>`;
    sheet.classList.remove("hidden");
    document.getElementById("sheet-close").onclick = () => sheet.classList.add("hidden");
    return;
  }
  const lines = state.cart
    .map((item) => `${item.qty}× ${item.name} — ${money(item.price * item.qty)}`)
    .join("<br>");
  const total = state.cart.reduce((sum, item) => sum + item.price * item.qty, 0);
  sheetCard.innerHTML = `
    <h3>Your cart</h3>
    <p class="lede">${lines}</p>
    <p class="price">Total ${money(total)}</p>
    <p class="meta">Checkout SMF SHOP bot ke Shop All se complete hota hai — yahan live catalog hai.</p>
    <div class="sheet-actions">
      <button class="ghost" id="sheet-clear">Clear</button>
      <button class="add" id="sheet-close">Close</button>
    </div>
  `;
  sheet.classList.remove("hidden");
  document.getElementById("sheet-close").onclick = () => sheet.classList.add("hidden");
  document.getElementById("sheet-clear").onclick = () => {
    state.cart = [];
    saveCart();
    sheet.classList.add("hidden");
  };
});

search.addEventListener("input", () => {
  state.query = search.value;
  renderGrid();
});

async function load() {
  grid.innerHTML = `<article class="card"><div class="name">Loading live catalog…</div></article>`;
  const [catRes, prodRes] = await Promise.all([
    fetch("/api/web/categories", { cache: "no-store" }),
    fetch("/api/web/products", { cache: "no-store" }),
  ]);
  if (!catRes.ok || !prodRes.ok) {
    empty.textContent = "Catalog is updating. Open Shop All in the bot if this stays empty.";
    empty.classList.remove("hidden");
    grid.innerHTML = "";
    return;
  }
  state.categories = await catRes.json();
  state.products = await prodRes.json();
  renderCats();
  renderGrid();
}

saveCart();
load().catch(() => {
  empty.textContent = "Could not load live products.";
  empty.classList.remove("hidden");
  grid.innerHTML = "";
});
