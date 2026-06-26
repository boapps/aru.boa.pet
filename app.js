"use strict";

// Maps a status legend index (its position in data.statuses) to a stable color
// class 0..3 based on the emoji, so colors stay consistent regardless of the
// order statuses happen to appear in a given snapshot.
const EMOJI_CLASS = { "🟢": 0, "🟡": 1, "🔴": 2, "⚪️": 3, "⚪": 3 };
const CLASS_LABEL = ["Készleten", "Korlátozott", "Nincs készlet", "Nincs értékesítés"];
const CLASS_COLOR = ["#16a34a", "#f59e0b", "#dc2626", "#9aa3ad"];

const state = {
  data: null,
  classOf: [],      // status index -> color class 0..3
  selected: 0,
  search: "",
  hidden: new Set(), // hidden color classes
  browsing: false,  // mobile: is the product list expanded?
};

const $ = (id) => document.getElementById(id);

init();

async function init() {
  try {
    const res = await fetch("data/latest.json", { cache: "no-store" });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    state.data = await res.json();
  } catch (err) {
    showError(err);
    return;
  }

  // Pre-compute the color class for each status legend entry.
  state.classOf = state.data.statuses.map((s) =>
    EMOJI_CLASS[s.emoji] !== undefined ? EMOJI_CLASS[s.emoji] : 3
  );

  buildMeta();
  buildProductList();
  buildStatusFilters();
  wireControls();

  // Reveal before first render so Leaflet measures a sized, visible container.
  $("app").hidden = false;

  // Deep link: #<cikkszám> selects that product on load, otherwise the first.
  const fromHash = productIndexFromHash();
  selectProduct(fromHash >= 0 ? fromHash : 0);
  window.addEventListener("hashchange", () => {
    const i = productIndexFromHash();
    if (i >= 0 && i !== state.selected) selectProduct(i);
  });

  if (map) map.invalidateSize();
}

// Resolve location.hash (#<cikkszám>) to a product index, or -1 if none match.
function productIndexFromHash() {
  const id = decodeURIComponent(location.hash.replace(/^#/, "")).trim();
  if (!id) return -1;
  return state.data.products.findIndex((p) => String(p.id) === id);
}

function showError(err) {
  const el = $("error");
  el.hidden = false;
  el.innerHTML =
    `<strong>Nem sikerült betölteni az adatokat.</strong><br>` +
    `Indítsd a weboldalt HTTP-kiszolgálóval a projekt gyökeréből, pl. ` +
    `<code>python3 -m http.server</code>, majd nyisd meg a ` +
    `<code>http://localhost:8000/</code> címet. (${escapeHtml(err.message)})`;
  $("meta").textContent = "Hiba történt.";
}

function buildMeta() {
  const d = state.data;
  const when = new Date(d.fetched_at);
  const abs = when.toLocaleString("hu-HU", {
    year: "numeric", month: "long", day: "numeric",
    hour: "2-digit", minute: "2-digit",
  });
  const el = $("meta");
  el.textContent =
    `${d.products.length} termék · ${d.stores.length} áruház · frissítve: ${relativeTime(when)}`;
  el.title = abs; // exact timestamp on hover
}

// Hungarian colloquial relative time, e.g. "1 órája", "5 perce", "3 napja".
// (Intl.RelativeTimeFormat only yields the longer "1 órával ezelőtt" form.)
const REL_UNITS = [
  [60, 1, "másodperce"],
  [3600, 60, "perce"],
  [86400, 3600, "órája"],
  [2592000, 86400, "napja"],
  [31536000, 2592000, "hónapja"],
  [Infinity, 31536000, "éve"],
];
function relativeTime(date) {
  const sec = Math.round((Date.now() - date.getTime()) / 1000); // >0 = past
  if (sec < 0) return "épp most";
  if (sec < 45) return "néhány másodperce";
  for (const [limit, div, word] of REL_UNITS) {
    if (sec < limit) return `${Math.round(sec / div)} ${word}`;
  }
}

// ---- Product list ---------------------------------------------------------

function productName(p) {
  return p.name || `Cikkszám ${p.id}`;
}

function buildProductList() {
  const ul = $("product-list");
  ul.innerHTML = "";
  state.data.products.forEach((p, i) => {
    const li = document.createElement("li");
    const counts = classCounts(p);
    const total = p.store_count || 0;

    const segs = [0, 1, 2, 3]
      .map((c) => {
        const pct = total ? (counts[c] / total) * 100 : 0;
        return pct ? `<span class="s-${c}" style="width:${pct}%"></span>` : "";
      })
      .join("");

    const thumb = p.image
      ? `<img class="card-thumb" src="${escapeHtml(p.image)}" alt="" loading="lazy">`
      : `<span class="card-thumb card-thumb-empty" aria-hidden="true"></span>`;

    li.innerHTML =
      `<button class="product-card" data-i="${i}">
        <div class="card-head">
          ${thumb}
          <div class="card-head-text">
            <p class="name">${escapeHtml(productName(p))}</p>
            <span class="id">Cikkszám: ${escapeHtml(p.id)}</span>
          </div>
        </div>
        <div class="summary"><b>${p.in_stock_count}</b> / ${total} áruházban kapható</div>
        <div class="bar">${segs}</div>
      </button>`;
    ul.appendChild(li);
  });

  ul.addEventListener("click", (e) => {
    const btn = e.target.closest(".product-card");
    if (btn) selectProduct(Number(btn.dataset.i));
  });
}

// Count stores per color class for a product.
function classCounts(p) {
  const counts = [0, 0, 0, 0];
  for (const code of p.avail) {
    if (code === null || code === undefined) continue;
    counts[state.classOf[code]]++;
  }
  return counts;
}

// ---- Status filters -------------------------------------------------------

function buildStatusFilters() {
  const box = $("status-filters");
  box.innerHTML = "";
  for (let c = 0; c < 4; c++) {
    const label = document.createElement("label");
    label.className = "sf";
    label.dataset.c = c;
    label.innerHTML =
      `<input type="checkbox" checked><span class="dot bg-${c}"></span>${CLASS_LABEL[c]}`;
    label.querySelector("input").addEventListener("change", (e) => {
      if (e.target.checked) state.hidden.delete(c);
      else state.hidden.add(c);
      label.classList.toggle("off", !e.target.checked);
      renderStores();
      renderMap();
    });
    box.appendChild(label);
  }
}

function wireControls() {
  $("search").addEventListener("input", (e) => {
    state.search = e.target.value.trim().toLowerCase();
    renderStores();
  });
  $("product-search").addEventListener("input", (e) => {
    filterProducts(e.target.value.trim().toLowerCase());
  });
  $("product-toggle").addEventListener("click", () => {
    setBrowsing(!state.browsing);
  });
}

// On narrow screens the product list stacks above the detail, so keep it
// collapsed once a product is chosen and surface a toggle to reopen it.
// (On desktop the list sits in its own column and is always shown.)
function setBrowsing(on) {
  state.browsing = on;
  const app = $("app");
  app.classList.toggle("browsing", on);

  const p = state.data.products[state.selected];
  const btn = $("product-toggle");
  btn.setAttribute("aria-expanded", String(on));
  btn.textContent = on
    ? "Lista bezárása ▴"
    : `${productName(p)} — másik termék ▾`;

  if (on) $("product-search").focus();
}

// Show/hide product cards matching the query (by name or article number).
function filterProducts(q) {
  let visible = 0;
  document.querySelectorAll("#product-list > li").forEach((li, i) => {
    const p = state.data.products[i];
    const hay = `${productName(p)} ${p.id}`.toLowerCase();
    const match = !q || hay.includes(q);
    li.hidden = !match;
    if (match) visible++;
  });
  $("product-empty").hidden = visible > 0;
}

// ---- Selection ------------------------------------------------------------

function selectProduct(i) {
  state.selected = i;
  $("app").classList.add("has-selection");
  $("product-toggle").hidden = false;
  setBrowsing(false); // collapse the list (mobile) now that a product is chosen
  document.querySelectorAll(".product-card").forEach((b) => {
    const active = Number(b.dataset.i) === i;
    b.classList.toggle("active", active);
    // Bring the chosen card into view (e.g. when arriving via a deep link),
    // without jumping if it is already visible.
    if (active) b.scrollIntoView({ block: "nearest" });
  });

  const p = state.data.products[i];

  // Keep the URL pointing at the selected product so it can be linked/bookmarked.
  // replaceState (not location.hash =) avoids flooding history on every click.
  const hash = "#" + encodeURIComponent(p.id);
  if (location.hash !== hash) history.replaceState(null, "", hash);

  $("detail-name").textContent = productName(p);

  // Main picture + link to the product page on the retailer's site (both optional).
  const thumb = $("detail-thumb");
  const img = $("detail-img");
  if (p.image) {
    img.src = p.image;
    img.alt = productName(p);
    thumb.hidden = false;
    if (p.url) thumb.href = p.url; else thumb.removeAttribute("href");
  } else {
    thumb.hidden = true;
    img.removeAttribute("src");
  }
  const link = $("detail-link");
  if (p.url) { link.href = p.url; link.hidden = false; }
  else { link.hidden = true; }

  $("detail-sub").textContent = p.store_count
    ? `${p.in_stock_count} / ${p.store_count} áruházban kapható`
    : "Ehhez a cikkszámhoz nincs elérhető készletadat.";

  // Status breakdown chips.
  const counts = classCounts(p);
  $("detail-chips").innerHTML = [0, 1, 2, 3]
    .filter((c) => counts[c] > 0)
    .map(
      (c) =>
        `<span class="chip"><span class="dot bg-${c}"></span>${CLASS_LABEL[c]} <b>${counts[c]}</b></span>`
    )
    .join("");

  renderMap();
  renderStores();
}

// ---- Map (Leaflet) --------------------------------------------------------

const DOT_R = 6;
let map = null;
let markerLayer = null;
let markersByIdx = {};

function initMap() {
  map = L.map("map", { scrollWheelZoom: true });
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
  }).addTo(map);
  markerLayer = L.layerGroup().addTo(map);

  // Fit once to all known stores; the view then stays stable across product /
  // filter changes so toggling never re-zooms the map under the user.
  const pts = state.data.stores
    .filter((s) => s.lat && s.lon)
    .map((s) => [s.lat, s.lon]);
  if (pts.length) map.fitBounds(pts, { padding: [24, 24] });
  else map.setView([47.16, 19.5], 7);
}

function renderMap() {
  if (!map) initMap();
  markerLayer.clearLayers();
  markersByIdx = {};

  const p = state.data.products[state.selected];
  state.data.stores.forEach((s, idx) => {
    const code = p.avail[idx];
    if (code === null || code === undefined) return; // not sold here
    const cls = state.classOf[code];
    if (state.hidden.has(cls)) return;
    const st = state.data.statuses[code];

    const m = L.circleMarker([s.lat, s.lon], dotStyle(cls, s.open));
    const head = [s.postcode, s.city].filter(Boolean).join(" ") || "Külföldi áruház";
    m.bindTooltip(
      `<b>${escapeHtml(head)}</b><br>${escapeHtml(s.street)}<br>` +
        `${st.emoji} ${escapeHtml(st.label)}` +
        (s.open ? "" : '<br><span style="color:#dc2626">zárva</span>'),
      { direction: "top" }
    );
    m.on("mouseover", () => setHot(idx));
    m.on("mouseout", clearHot);
    m.on("click", () => {
      const row = document.querySelector(`.store-row[data-idx="${idx}"]`);
      if (row) row.scrollIntoView({ block: "center", behavior: "smooth" });
      setHot(idx);
    });
    m.addTo(markerLayer);
    markersByIdx[idx] = m;
  });
}

// Closed stores get a dark outline; open stores a white one.
function dotStyle(cls, open) {
  return {
    radius: DOT_R,
    fillColor: CLASS_COLOR[cls],
    fillOpacity: 0.9,
    color: open ? "#fff" : "#15202b",
    weight: open ? 1.2 : 2,
    opacity: 1,
  };
}

// ---- Store list -----------------------------------------------------------

function renderStores() {
  const ul = $("store-list");
  const p = state.data.products[state.selected];
  const stores = state.data.stores;
  const q = state.search;

  // Build a sortable, filtered view.
  const rows = [];
  stores.forEach((s, idx) => {
    const code = p.avail[idx];
    if (code === null || code === undefined) return;
    const cls = state.classOf[code];
    if (state.hidden.has(cls)) return;
    if (q) {
      const hay = `${s.city} ${s.postcode} ${s.street}`.toLowerCase();
      if (!hay.includes(q)) return;
    }
    rows.push({ s, idx, code, cls });
  });

  rows.sort((a, b) =>
    (a.s.city || "Ω").localeCompare(b.s.city || "Ω", "hu") ||
    a.s.street.localeCompare(b.s.street, "hu")
  );

  $("store-empty").hidden = rows.length > 0;

  ul.innerHTML = rows
    .map(({ s, idx, code }) => {
      const st = state.data.statuses[code];
      const pc = s.postcode ? `<span class="pc">${escapeHtml(s.postcode)}</span> ` : "";
      const city = s.city || "Külföldi áruház";
      const closed = s.open ? "" : `<span class="closed">zárva</span>`;
      return (
        `<li class="store-row" data-idx="${idx}">
          <span class="emoji">${st.emoji}</span>
          <span class="where">
            <span class="city">${pc}${escapeHtml(city)}${closed}</span>
            <span class="addr">${escapeHtml(s.street)}</span>
          </span>
          <span class="stat">${escapeHtml(st.label)}</span>
        </li>`
      );
    })
    .join("");

  // Hover a row -> highlight its map dot.
  ul.onmousemove = (e) => {
    const row = e.target.closest(".store-row");
    if (row) setHot(Number(row.dataset.idx));
  };
  ul.onmouseleave = clearHot;
}

// ---- Highlight linking (map marker <-> list row) --------------------------

let hotIdx = null;
function setHot(idx) {
  if (idx === hotIdx) return;
  clearHot();
  hotIdx = idx;
  const m = markersByIdx[idx];
  if (m) {
    m.setStyle({ radius: DOT_R + 4, weight: 2.5 });
    m.bringToFront();
    m.openTooltip();
  }
  const row = document.querySelector(`.store-row[data-idx="${idx}"]`);
  if (row) row.classList.add("hot");
}
function clearHot() {
  if (hotIdx !== null) {
    const m = markersByIdx[hotIdx];
    const s = state.data.stores[hotIdx];
    if (m && s) { m.setStyle({ radius: DOT_R, weight: s.open ? 1.2 : 2 }); m.closeTooltip(); }
    const row = document.querySelector(`.store-row[data-idx="${hotIdx}"]`);
    if (row) row.classList.remove("hot");
    hotIdx = null;
  }
}

// ---- utils ----------------------------------------------------------------

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (ch) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch])
  );
}
