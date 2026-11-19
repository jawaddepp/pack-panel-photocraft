"use strict";

const TOKEN = document.querySelector('meta[name="panel-token"]').content;
const NATIVE = window.webkit?.messageHandlers?.packpanel || null;

const KIND = {
  image:    { verb: "Place",   label: "Image" },
  document: { verb: "Open",    label: "PSD" },
  brushes:  { verb: "Import",  label: "Brushes" },
  patterns: { verb: "Import",  label: "Patterns" },
  styles:   { verb: "Import",  label: "Styles" },
  actions:  { verb: "Import",  label: "Actions" },
  font:     { verb: "Install", label: "Font" },
};
const ALPHA_EXT = new Set(["png", "tif", "tiff", "webp", "gif", "svg", "tga", "avif"]);
const PRIMARY_ACTION = { Place: "place", Open: "open", Import: "import", Install: "install" };
const PROGRESS = { place: "Placing", open: "Opening", import: "Importing", reimport: "Importing", install: "Installing" };

const $ = (id) => document.getElementById(id);
const ui = {
  main: $("main"), brand: $("brand"), linkDot: $("link-dot"), linkSr: $("link-sr"),
  linkBar: $("link-bar"), linkText: $("link-text"), linkAction: $("link-action"),
  favFilter: $("fav-filter"), recentBtn: $("recent-btn"), recentPop: $("recent-pop"), recentList: $("recent-list"),
  search: $("search"), searchWrap: $("search-wrap"), tabs: [...document.querySelectorAll(".tab")],
  toast: $("toast"), toastText: $("toast-text"), menu: $("menu"),
  settings: $("settings"), rootPath: $("root-path"), fitToggle: $("fit-toggle"), inspoUrl: $("inspo-url"), version: $("version"),
  storeUrl: $("store-url"), storeDot: $("store-dot"), dlBar: $("dl-bar"), dlText: $("dl-text"), dlFill: $("dl-fill"),
  storeDlg: $("store"), storeList: $("store-list"),
};

const store = {
  state: null,
  library: null,
  byRel: new Map(),
  packOfRel: new Map(),
  tab: "packs",
  favOnly: false,
  query: "",
  expanded: new Set(readLocal("expanded", [])),
  openPack: readLocal("openPack", null),
  busy: false,
  libraryError: "",
  shop: null,          // full store state with items (from /api/store)
  shopCompleted: null, // download counter, to reload the library when packs arrive
  signinEmail: "",
};

function readLocal(key, fallback) {
  try { const v = localStorage.getItem(`packpanel.${key}`); return v ? JSON.parse(v) : fallback; } catch { return fallback; }
}
function writeLocal(key, value) {
  try { localStorage.setItem(`packpanel.${key}`, JSON.stringify(value)); } catch { /* storage unavailable */ }
}

/* ------------------------------------------------------------ network */

async function api(path, body) {
  const opts = { headers: { "X-Panel-Token": TOKEN } };
  if (body !== undefined) {
    opts.method = "POST";
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  let res;
  try {
    res = await fetch(path, opts);
  } catch {
    throw new Error("Pack Panel stopped running. Open it again from its app icon.");
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const err = new Error(data.error || `Request failed (${res.status}).`);
    err.data = data;
    throw err;
  }
  return data;
}

// size = short side in px for tiles ("fill"), long side for the large preview ("fit").
const thumbUrl = (rel, size, mode = "fill") => `/thumb?p=${encodeURIComponent(rel)}&s=${size}&m=${mode}&t=${encodeURIComponent(TOKEN)}`;
const TILE_PX = 480;     // sharp on Retina for tiles up to 240 pt
const PREVIEW_PX = 2048; // large preview, never above the original

/* ------------------------------------------------------------- helpers */

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === undefined || v === null || v === false) continue;
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) if (c != null) node.append(c);
  return node;
}

function icon(id) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("aria-hidden", "true");
  const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
  use.setAttribute("href", `#i-${id}`);
  svg.append(use);
  return svg;
}

let toastTimer;
let toastHold = false;
/**
 * Show a status message. `opts.actions` adds buttons ([{ label, run }]); `opts.where` adds a
 * second line saying where something is (e.g. "Window › Styles").
 */
function toast(text, kind = "info", opts = {}) {
  clearTimeout(toastTimer);
  if (!text) { ui.toast.hidden = true; document.body.classList.remove("has-toast"); syncInspoFrame(); return; }
  ui.toast.hidden = false;
  document.body.classList.add("has-toast");
  ui.toast.dataset.kind = kind;
  ui.toastText.textContent = text;
  const where = $("toast-where");
  where.textContent = opts.where || "";
  where.hidden = !opts.where;
  const actions = $("toast-actions");
  actions.replaceChildren(...(opts.actions || []).map((a) =>
    el("button", { class: "btn btn-small", type: "button", onclick: () => { toast(""); a.run(); } }, a.label)));
  if (kind !== "busy") scheduleToastHide(kind === "error" ? 8000 : (opts.actions?.length ? 9000 : Math.max(2600, text.length * 55)));
  syncInspoFrame();
}
function scheduleToastHide(ms) {
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    if (toastHold) { scheduleToastHide(1500); return; }
    ui.toast.hidden = true;
    document.body.classList.remove("has-toast");
    syncInspoFrame();
  }, ms);
}
ui.toast.addEventListener("pointerenter", () => { toastHold = true; });
ui.toast.addEventListener("pointerleave", () => { toastHold = false; });
ui.toast.addEventListener("focusin", () => { toastHold = true; });
ui.toast.addEventListener("focusout", () => { toastHold = false; });

async function showInPhotoCraft(panel) {
  try {
    const res = await api("/api/show", { panel });
    toast(res.message);
  } catch (err) {
    toast(err.message, "error");
  }
}

async function undoLast() {
  try {
    const res = await api("/api/undo", {});
    toast(res.message);
  } catch (err) {
    toast(err.message, "error");
  }
}

const favorites = () => new Set(store.state?.config.favorites || []);
const installed = () => new Set(store.state?.installed || []);
const installedKey = () => (store.state?.installed || []).join("\n");
const packOf = (asset) => store.packOfRel.get(asset.rel);
/**
 * Transparent art can be dark (black topography lines) or light (white smoke). Measure the
 * opaque pixels once the preview loads and pick a backdrop it shows up on.
 */
const artTone = new Map();
function fitBackdrop(img, holder) {
  const apply = (tone) => {
    holder.classList.toggle("is-dark-art", tone === "dark");
    holder.classList.toggle("is-light-art", tone === "light");
  };
  if (artTone.has(img.src)) { apply(artTone.get(img.src)); return; }
  const run = () => {
    try {
      const c = document.createElement("canvas");
      c.width = c.height = 48;
      const g = c.getContext("2d", { willReadFrequently: true });
      g.drawImage(img, 0, 0, 48, 48);
      const px = g.getImageData(0, 0, 48, 48).data;
      let sum = 0, n = 0;
      for (let i = 0; i < px.length; i += 4) {
        if (px[i + 3] > 40) { sum += 0.2126 * px[i] + 0.7152 * px[i + 1] + 0.0722 * px[i + 2]; n++; }
      }
      const mean = n ? sum / n : 128;
      const tone = mean < 95 ? "dark" : mean > 165 ? "light" : "mid";
      artTone.set(img.src, tone);
      apply(tone);
    } catch { /* unreadable image: keep the theme backdrop */ }
  };
  if (img.complete && img.naturalWidth) run(); else img.addEventListener("load", run, { once: true });
}

/** "2" becomes "Bitmap Textures 2" where the pack isn't obvious (recent list, favorites, preview). */
function displayName(asset) {
  const pack = packOf(asset);
  return pack && /^[\d\s._()\[\]-]{1,6}$/.test(asset.name) ? `${pack.name} ${asset.name.replace(/[\[\]()]/g, "")}` : asset.name;
}
/** The picture for an asset: its own preview, else its pack's store cover (brushes, styles). */
function assetPicture(asset, size) {
  if (asset.thumb) return thumbUrl(asset.rel, size);
  const pack = packOf(asset);
  if (pack?.storeId && shopItem(pack.storeId)?.hasCover) return coverUrl(pack.storeId);
  return null;
}

/* -------------------------------------------------------- connection UI */

function renderLink() {
  const s = store.state;
  if (!s) return;
  const c = s.connection;
  let text = "", action = null, tone = "off";
  if (c.state === "connected" && !c.needsRelaunch) {
    tone = "connected";
  } else if (c.state === "connected") {
    text = "Your packs folder changed. Reconnect so PhotoCraft can read it."; action = ["Reconnect", "relaunch"]; tone = "attention";
  } else if (c.state === "running-unlinked") {
    text = "PhotoCraft is open without the panel link."; action = ["Reconnect", "relaunch"]; tone = "attention";
  } else if (c.state === "closed") {
    text = "PhotoCraft is closed."; action = ["Open PhotoCraft", "connect"];
  } else {
    text = "PhotoCraft isn’t installed in Applications.";
  }
  if (!s.config.assetsRoot) action = null;
  ui.linkDot.dataset.state = tone;
  ui.linkSr.textContent = tone === "connected" ? "Connected to PhotoCraft" : text;
  ui.brand.title = tone === "connected" ? "Pack Panel menu. Connected to PhotoCraft" : `Pack Panel menu. ${text}`;
  ui.linkBar.hidden = tone === "connected" || !s.config.assetsRoot;
  ui.linkText.textContent = text;
  ui.linkAction.hidden = !action;
  if (action) { ui.linkAction.textContent = action[0]; ui.linkAction.dataset.op = action[1]; }
  syncInspoFrame();
}

async function connect(op) {
  ui.linkAction.disabled = true;
  toast(op === "relaunch" ? "Restarting PhotoCraft with the panel link…" : "Opening PhotoCraft…", "busy");
  try {
    store.state = await api(`/api/${op}`, {});
    renderLink();
    toast("Connected to PhotoCraft");
  } catch (err) {
    toast(err.message, "error");
  } finally {
    ui.linkAction.disabled = false;
  }
}

/* --------------------------------------------------------------- views */

function splitLibrary() {
  const packs = [], templates = [];
  for (const pack of store.library?.packs || []) {
    const docs = pack.assets.filter((a) => a.kind === "document");
    const rest = pack.assets.filter((a) => a.kind !== "document");
    if (docs.length) templates.push(...docs);
    if (rest.length) packs.push({ ...pack, assets: rest, count: rest.length });
  }
  return { packs, templates };
}

function render() {
  for (const tab of ui.tabs) tab.setAttribute("aria-selected", String(tab.dataset.tab === store.tab));
  ui.searchWrap.hidden = store.tab === "inspo";
  ui.favFilter.setAttribute("aria-pressed", String(store.favOnly));
  const main = ui.main;
  main.replaceChildren();
  main.classList.toggle("is-inspo", store.tab === "inspo");

  if (store.tab === "inspo") { renderInspo(); return; }
  syncInspoFrame();

  if (!store.state || !store.library) {
    main.append(el("ul", { class: "rows", "aria-busy": "true", "aria-label": "Loading packs" },
      ...Array.from({ length: 7 }, () => el("li", { class: "skeleton-row" }, el("span", { class: "sk-cover" }),
        el("span", { class: "sk-lines" }, el("span", { class: "sk-line" }), el("span", { class: "sk-line is-short" }))))));
    return;
  }
  if (store.libraryError) {
    main.append(emptyState("Couldn’t read the packs folder", store.libraryError, "Try again", loadLibrary));
    return;
  }
  const lib = store.library;
  if (!store.state.config.assetsRoot || lib.rootMissing) {
    main.append(emptyState(
      lib.rootMissing ? "Packs folder not found" : "Choose your packs folder",
      lib.rootMissing
        ? `${store.state.config.assetsRoot} was moved or renamed. Choose where it lives now.`
        : "Choose the folder that holds your asset packs. Each subfolder becomes a pack you can browse here.",
      "Choose folder…", chooseFolder));
    return;
  }
  if (!lib.packs.length) {
    main.append(emptyState("No assets in this folder",
      `Pack Panel looks for images, PSDs, brushes, patterns, styles and actions inside ${lib.root}.`,
      "Change folder…", chooseFolder));
    return;
  }

  const { packs, templates } = splitLibrary();
  if (store.tab === "templates") return renderTemplates(templates);
  if (store.favOnly) return renderFavorites();
  if (store.query) return renderSearch(packs);
  renderPacks(packs);
}

function clearSearch() {
  ui.search.value = "";
  store.query = "";
  render();
  ui.search.focus();
}

function emptyState(title, body, cta, onclick) {
  return el("div", { class: "empty" },
    el("h2", { text: title }),
    el("p", { text: body }),
    cta ? el("button", { class: "btn btn-primary", type: "button", onclick }, cta) : null);
}

const coverUrl = (storeId) => `/cover?id=${encodeURIComponent(storeId)}&t=${encodeURIComponent(TOKEN)}`;

function shopItems(kinds) {
  return (store.shop?.items || []).filter((i) => kinds.includes(i.kind));
}
const shopItem = (id) => (store.shop?.items || []).find((i) => i.id === id);

function signInPrompt() {
  const sum = store.state?.store;
  if (!sum?.configured || sum.signedIn || readLocal("signinDismissed", false)) return null;
  const box = el("div", { class: "notice" },
    el("p", {}, el("strong", { text: "Sign in to your store account" }), " to download new packs and keep yours updated."),
    el("button", { class: "btn btn-small btn-primary", type: "button", onclick: openStore }, "Sign in"),
    el("button", { class: "btn btn-small btn-link", type: "button",
      onclick: () => { writeLocal("signinDismissed", true); box.remove(); toast("You can sign in any time from the Pack Panel menu"); } }, "Not now"));
  return box;
}

function formatBytes(n) {
  if (!n) return "";
  const gb = n / 1073741824;
  if (gb >= 1) return `${gb.toFixed(gb >= 10 ? 0 : 1)} GB`;
  const mb = n / 1048576;
  return `${mb >= 10 ? Math.round(mb) : mb.toFixed(1)} MB`;
}

function cardImage(storeId, fallbackRel) {
  const item = storeId ? shopItem(storeId) : null;
  if (item?.hasCover) return el("img", { class: "card-img", src: coverUrl(storeId), alt: "", loading: "lazy", decoding: "async" });
  if (fallbackRel) return el("img", { class: "card-img", src: thumbUrl(fallbackRel, 480), alt: "", loading: "lazy", decoding: "async" });
  return el("span", { class: "card-img card-img-empty", "aria-hidden": "true" });
}

function rowCover(storeId, fallbackRel) {
  const item = storeId ? shopItem(storeId) : null;
  if (item?.hasCover) return el("img", { class: "row-cover", src: coverUrl(storeId), alt: "", loading: "lazy", decoding: "async" });
  if (fallbackRel) return el("img", { class: "row-cover", src: thumbUrl(fallbackRel, 300), alt: "", loading: "lazy", decoding: "async" });
  return el("span", { class: "row-cover", "aria-hidden": "true" });
}

/** A downloaded pack as a dropdown row with the store's cover art. Only one row is open at a time. */
function packRow(pack, open) {
  const item = pack.storeId ? shopItem(pack.storeId) : null;
  const meta = [`${pack.count} ${pack.count === 1 ? "file" : "files"}`, item ? formatBytes(item.bytes) : ""].filter(Boolean).join(", ");
  const contentId = `g-${cssId(pack.id)}`;
  const header = el("button", { class: "row", type: "button", "aria-expanded": String(open), "aria-controls": contentId,
    "data-pack": pack.id, onclick: () => togglePack(pack.id) },
    rowCover(pack.storeId, pack.banner),
    el("span", { class: "row-text" },
      el("span", { class: "row-name", text: pack.name }),
      el("span", { class: "row-meta", text: meta })),
    item?.status === "update" ? el("span", { class: "row-badge is-update", text: "Update" })
      : item?.isNew ? el("span", { class: "row-badge", text: "New" }) : null,
    el("span", { class: "row-chevron", "aria-hidden": "true" }, icon("chevron")));
  const content = el("div", { class: "group-content", id: contentId, role: "region", "aria-label": pack.name });
  content.hidden = !open;
  if (open) {
    if (item?.status === "update") {
      content.append(el("div", { class: "notice" }, el("p", { text: "A newer version of this pack is in your store account." }),
        el("button", { class: "btn btn-small btn-primary", type: "button", onclick: () => shopDownload([item.id]) }, "Update")));
    }
    content.append(grid(pack.assets, favorites(), false));
  }
  return el("li", { class: `group${open ? " is-open" : ""}` }, header, content);
}

/** A pack in the store account that isn't downloaded yet, as a row with a Download button. */
function cloudRow(it) {
  const cur = store.shop?.current;
  const pct = cur && cur.id === it.id && cur.total ? Math.min(100, Math.round((cur.done / cur.total) * 100)) : null;
  let action;
  if (it.status === "downloading") action = el("span", { class: "card-state", text: pct === null ? "Starting…" : `${pct}%` });
  else if (it.status === "queued") action = el("span", { class: "card-state", text: "Queued" });
  else if (!it.ready) action = el("span", { class: "card-state", text: "Preparing" });
  else action = el("button", { class: "btn btn-small btn-primary", type: "button", onclick: () => shopDownload([it.id]) },
    icon("cloud"), it.status === "failed" ? "Retry" : "Download");
  const meta = [it.files ? `${it.files} files` : "", formatBytes(it.bytes), "not downloaded"].filter(Boolean).join(", ");
  const row = el("div", { class: `row is-cloud${it.status === "downloading" ? " is-loading" : ""}`, title: it.error || "" },
    rowCover(it.id, null),
    el("span", { class: "row-text" }, el("span", { class: "row-name", text: it.name }), el("span", { class: "row-meta", text: meta })),
    it.isNew ? el("span", { class: "row-badge", text: "New" }) : null,
    el("span", { class: "row-action" }, action));
  if (pct !== null) {
    const fill = el("span", { class: "strip-fill" });
    fill.style.width = `${pct}%`;
    row.append(el("span", { class: "strip-progress" }, fill));
  }
  return el("li", { class: "group" }, row);
}

/** A pack in the store account that isn't downloaded yet. */
function cloudCard(it) {
  const cur = store.shop?.current;
  const pct = cur && cur.id === it.id && cur.total ? Math.min(100, Math.round((cur.done / cur.total) * 100)) : null;
  let action;
  if (it.status === "downloading") action = el("span", { class: "card-state", text: pct === null ? "Starting…" : `Downloading ${pct}%` });
  else if (it.status === "queued") action = el("span", { class: "card-state", text: "Queued" });
  else if (!it.ready) action = el("span", { class: "card-state", text: "Being prepared" });
  else action = el("button", { class: "btn btn-small btn-block", type: "button", onclick: () => shopDownload([it.id]) },
    icon("cloud"), it.status === "failed" ? "Retry download" : "Download");
  const meta = [it.files ? `${it.files} files` : "", formatBytes(it.bytes)].filter(Boolean).join(", ");
  const card = el("div", { class: `card is-cloud${it.status === "downloading" ? " is-loading" : ""}`, title: it.error || "" },
    el("span", { class: "card-media" }, cardImage(it.id, null), it.isNew ? el("span", { class: "card-badge", text: "New" }) : null),
    el("span", { class: "card-body" },
      el("span", { class: "card-name", text: it.name }),
      el("span", { class: "card-meta", text: meta }),
      el("span", { class: "card-action" }, action)));
  if (pct !== null) {
    const fill = el("span", { class: "strip-fill" });
    fill.style.width = `${pct}%`;
    card.append(el("span", { class: "strip-progress" }, fill));
  }
  card.dataset.id = it.id;
  return el("li", {}, card);
}

function pendingItems(kinds) {
  if (!(store.shop?.signedIn ?? store.state?.store?.signedIn)) return [];
  return shopItems(kinds).filter((i) => i.status !== "installed" && i.status !== "update");
}

function downloadAllBar(kinds) {
  const pending = pendingItems(kinds).filter((i) => i.ready && (i.status === "missing" || i.status === "failed"));
  const updates = shopItems(kinds).filter((i) => i.status === "update");
  if (!pending.length && !updates.length) return null;
  const n = pending.length + updates.length;
  return el("div", { class: "notice" },
    el("p", {}, el("strong", { text: `${n} ${n === 1 ? "item" : "items"} to get` }),
      ` from your store account${updates.length ? `, including ${updates.length} update${updates.length === 1 ? "" : "s"}` : ""}.`),
    el("button", { class: "btn btn-small btn-primary", type: "button", onclick: () => shopDownload(null, "everything") }, "Download all"));
}

function renderPacks(packs) {
  const prompt = signInPrompt() || downloadAllBar(["pack", "brushes", "style"]);
  if (prompt) ui.main.append(prompt);
  const openId = store.openPack;
  const list = el("ul", { class: "rows", "aria-label": "Packs" });
  for (const pack of packs) list.append(packRow(pack, pack.id === openId));
  for (const it of pendingItems(["pack", "brushes", "style"])) list.append(cloudRow(it));
  ui.main.append(list);
}

/** Open a pack's dropdown, closing the one that was open, and bring it into view. */
function togglePack(id) {
  store.openPack = store.openPack === id ? null : id;
  writeLocal("openPack", store.openPack);
  rerender();
  const header = [...ui.main.querySelectorAll(".row[data-pack]")].find((b) => b.dataset.pack === id);
  if (header) {
    if (store.openPack) {
      const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
      header.scrollIntoView({ block: "start", behavior: reduce ? "auto" : "smooth" });
    }
    header.focus({ preventScroll: true });
  }
}

function openPack(id) {
  if (store.tab !== "packs") store.tab = "packs";
  store.openPack = null;
  togglePack(id);
}

function renderFavorites() {
  const assets = [...favorites()].map((r) => store.byRel.get(r)).filter(Boolean)
    .filter((a) => matches(a, words()));
  ui.main.append(el("h1", { class: "section-title", text: "Favorites" }));
  if (!assets.length) {
    ui.main.append(store.query
      ? emptyState("No favorites match", `Nothing in your favorites matches “${store.query}”.`, "Clear search", clearSearch)
      : emptyState("No favorites yet", "Hover any image and click the star to keep it here for quick access.", "Show all packs",
          () => { store.favOnly = false; render(); }));
    return;
  }
  ui.main.append(grid(assets, favorites(), true));
}

const words = () => store.query.toLowerCase().split(/\s+/).filter(Boolean);
function matches(asset, ws) {
  if (!ws.length) return true;
  const hay = `${asset.name} ${packOf(asset)?.name || ""} ${asset.ext} ${KIND[asset.kind].label}`.toLowerCase();
  return ws.every((w) => hay.includes(w));
}

function renderSearch(packs) {
  const ws = words(), favs = favorites();
  let total = 0;
  const out = [];
  for (const pack of packs) {
    const nameHit = ws.every((w) => pack.name.toLowerCase().includes(w));
    const hits = nameHit ? pack.assets : pack.assets.filter((a) => matches(a, ws));
    if (!hits.length) continue;
    total += hits.length;
    out.push(el("section", { class: "result-group" },
      el("h2", { class: "result-title" },
        rowCover(pack.storeId, pack.banner),
        el("button", { class: "link-btn", type: "button", onclick: () => { ui.search.value = ""; store.query = ""; openPack(pack.id); } }, pack.name),
        el("span", { class: "count", text: String(hits.length) })),
      grid(hits, favs, false)));
  }
  if (!total) {
    ui.main.append(emptyState("No matches", `Nothing matches “${store.query}”. Try a pack name, such as light leaks or smoke.`, "Clear search", clearSearch));
    return;
  }
  ui.main.append(el("h1", { class: "section-title", text: `${total} ${total === 1 ? "result" : "results"}` }), ...out);
}

function templateCard(asset) {
  const item = asset.storeId ? shopItem(asset.storeId) : null;
  const title = item?.name || asset.name;
  const meta = ["Template", item ? formatBytes(item.bytes) : asset.ext.toUpperCase()].filter(Boolean).join(", ");
  const media = el("span", { class: "card-media" }, cardImage(asset.storeId, asset.thumb ? asset.rel : null));
  return el("li", {},
    el("button", { class: "card", type: "button", title: `Open ${title}`, "aria-label": `Open ${title}`,
      onclick: () => act(asset, "open", media),
      oncontextmenu: (e) => { e.preventDefault(); openAssetMenu(asset, e.clientX, e.clientY, media); } },
      media,
      el("span", { class: "card-body" }, el("span", { class: "card-name", text: title }), el("span", { class: "card-meta", text: meta }))));
}

function renderTemplates(templates) {
  const ws = words();
  const shown = templates.filter((a) => matches(a, ws) || ws.every((w) => (shopItem(a.storeId)?.name || "").toLowerCase().includes(w)));
  const pending = pendingItems(["template"]);
  const bar = downloadAllBar(["template"]);
  if (bar) ui.main.append(bar);
  if (!templates.length && !pending.length) {
    ui.main.append(el("p", { class: "section-note", text: "No PSD or PSB files in your packs folder yet." }));
    return;
  }
  if (templates.length && !shown.length && !pending.length) {
    ui.main.append(el("p", { class: "section-note", text: `No templates match “${store.query}”.` }));
    return;
  }
  const cards = el("ul", { class: "cards", "aria-label": "Templates" });
  for (const a of shown) cards.append(templateCard(a));
  for (const it of pending) cards.append(cloudCard(it));
  ui.main.append(el("p", { class: "section-note", text: "Opens as a new document. Large layered files can take a moment." }), cards);
}

function renderInspo() {
  const url = store.state?.config.inspoUrl;
  if (!url) {
    ui.main.append(emptyState("No Inspo page set", "Add the address of a page you like to browse for ideas in Settings.", "Open settings", openSettings));
    syncInspoFrame();
    return;
  }
  if (NATIVE) {
    ui.main.append(el("div", { class: "inspo-host", id: "inspo-host" }));
    requestAnimationFrame(syncInspoFrame);
    return;
  }
  ui.main.append(emptyState("Inspo", "This page opens in your browser when Pack Panel runs outside its own app window.", "Open Inspo page",
    () => window.open(url, "_blank", "noopener")));
}

function syncInspoFrame() {
  if (!NATIVE) return;
  const covered = ui.settings.open || ui.storeDlg.open || !ui.menu.hidden || !ui.recentPop.hidden || $("preview")?.open || $("confirm")?.open;
  const show = store.tab === "inspo" && !!store.state?.config.inspoUrl && !covered;
  const rect = ui.main.getBoundingClientRect();
  const bottom = ui.toast.hidden ? 0 : Math.round(window.innerHeight - ui.toast.getBoundingClientRect().top + 8);
  try {
    NATIVE.postMessage({ type: "inspo", show, freeze: covered && store.tab === "inspo", url: store.state?.config.inspoUrl || "",
      top: Math.round(rect.top), bottom });
  } catch { /* native bridge unavailable */ }
}

function grid(assets, favs, showPack) {
  const list = el("ul", { class: "grid" });
  for (const a of assets) list.append(tile(a, favs.has(a.rel), showPack));
  return list;
}

function tile(asset, isFav, showPack) {
  const kind = KIND[asset.kind];
  const isInstalled = installed().has(asset.rel);
  // Brushes, styles and actions have no picture of their own: show their pack's store cover instead.
  const packCover = asset.thumb ? null : assetPicture(asset, 320);
  const thumb = el("span", { class: `tile-thumb${asset.thumb && ALPHA_EXT.has(asset.ext) ? " is-alpha" : ""}` },
    asset.thumb
      ? el("img", { src: thumbUrl(asset.rel, TILE_PX), alt: "", loading: "lazy", decoding: "async",
          onload: (e) => { if (ALPHA_EXT.has(asset.ext)) fitBackdrop(e.target, e.target.parentElement); },
          onerror: (e) => e.target.replaceWith(el("span", { class: "tile-ext", text: asset.ext.toUpperCase() })) })
      : packCover ? el("img", { src: packCover, alt: "", loading: "lazy", decoding: "async" })
      : el("span", { class: "tile-ext", text: asset.ext.toUpperCase() }),
    isInstalled
      ? el("span", { class: "tile-badge is-installed", text: "In PhotoCraft" })
      : asset.kind !== "image" ? el("span", { class: "tile-badge", text: kind.label }) : null);
  const label = isInstalled ? `${asset.name}, already in PhotoCraft` : `${kind.verb} ${asset.name}`;
  const main = el("button", {
    class: "tile-main", type: "button", title: label, "aria-label": label, "data-rel": asset.rel,
    onclick: () => act(asset, PRIMARY_ACTION[kind.verb], thumb),
    oncontextmenu: (e) => { e.preventDefault(); openAssetMenu(asset, e.clientX, e.clientY, thumb); },
    onkeydown: (e) => { if (e.key === " ") { e.preventDefault(); openPreview(asset, e.currentTarget); } },
  },
    thumb,
    el("span", { class: "tile-name", text: asset.name }),
    showPack ? el("span", { class: "tile-pack", text: packOf(asset)?.name || "" }) : null);

  const star = el("button", {
    class: "tile-tool", type: "button", "aria-pressed": String(isFav),
    "aria-label": isFav ? `Remove ${asset.name} from favorites` : `Add ${asset.name} to favorites`,
    onclick: () => toggleFavorite(asset, star),
  }, icon("star"));
  const more = el("button", {
    class: "tile-tool", type: "button", "aria-haspopup": "menu", "aria-label": `More actions for ${asset.name}`,
    onclick: (e) => { const r = e.currentTarget.getBoundingClientRect(); openAssetMenu(asset, r.right, r.bottom + 4, thumb, e.currentTarget); },
  }, icon("more"));

  const zoom = el("button", {
    class: "tile-tool", type: "button", "aria-label": `Preview ${asset.name} (Space)`, title: "Preview (Space)",
    onclick: (e) => openPreview(asset, e.currentTarget),
  }, icon("expand"));
  return el("li", { class: "tile" }, main, el("span", { class: "tile-tools" }, zoom, star, more));
}

const cssId = (s) => s.replace(/[^a-zA-Z0-9_-]/g, "_");

/* ------------------------------------------------------------- actions */

/** Ask a yes/no question in the panel's own dialog. Resolves true for the confirm button. */
function confirmDialog(title, body, okLabel) {
  const dlg = $("confirm");
  $("confirm-title").textContent = title;
  $("confirm-body").textContent = body;
  $("confirm-ok").textContent = okLabel;
  return new Promise((resolve) => {
    dlg.addEventListener("close", () => { syncInspoFrame(); resolve(dlg.returnValue === "ok"); }, { once: true });
    dlg.returnValue = "";
    dlg.showModal();
    syncInspoFrame();
    $("confirm-ok").focus();
  });
}

async function act(asset, action, thumbEl, approve = false) {
  if (store.busy) { toast("Hold on, PhotoCraft is still working on the last one."); return; }
  store.busy = true;
  thumbEl?.classList.add("is-busy");
  toast(`${PROGRESS[action] || "Working on"} ${displayName(asset)}…${approve ? " Large files can take a minute." : ""}`, "busy");
  try {
    const res = await api("/api/action", { rel: asset.rel, action, ...(approve ? { approve: true } : {}) });
    const before = installedKey();
    store.state = res.state;
    renderLink();
    const actions = [];
    if (res.reveal) actions.push({ label: "Show me", run: () => showInPhotoCraft(res.reveal.panel) });
    if (res.undo) actions.push({ label: "Undo", run: undoLast });
    toast(res.message, "info", { actions, where: res.reveal ? `In PhotoCraft: ${res.reveal.where}, group “${res.reveal.group}”` : "" });
    if (thumbEl && (action === "place" || action === "open")) {
      thumbEl.classList.add("is-done");
      setTimeout(() => thumbEl.classList.remove("is-done"), 1200);
    }
    if (installedKey() !== before) rerender();
    if (!ui.recentPop.hidden) renderRecent();
  } catch (err) {
    if (err.data?.needsApproval && !approve) {
      store.busy = false;
      thumbEl?.classList.remove("is-busy");
      toast("");
      const ram = err.data.ramGB ? ` Your Mac has ${err.data.ramGB} GB.` : "";
      const ok = await confirmDialog(
        "Open a large file?",
        `${(asset.storeId && shopItem(asset.storeId)?.name) || displayName(asset)} needs about ${err.data.neededGB} GB of memory in PhotoCraft.${ram} It can take a minute to open, and other apps may slow down while it's open.`,
        action === "place" ? "Place anyway" : "Open anyway");
      if (ok) return act(asset, action, thumbEl, true);
      return;
    }
    toast(err.message, "error");
    refreshState();
  } finally {
    store.busy = false;
    thumbEl?.classList.remove("is-busy");
  }
}

async function toggleFavorite(asset, button) {
  const on = button.getAttribute("aria-pressed") !== "true";
  button.setAttribute("aria-pressed", String(on));
  button.setAttribute("aria-label", `${on ? "Remove" : "Add"} ${asset.name} ${on ? "from" : "to"} favorites`);
  try {
    store.state = await api("/api/favorite", { rel: asset.rel, on });
    if (store.favOnly) rerender();
  } catch (err) {
    button.setAttribute("aria-pressed", String(!on));
    toast(err.message, "error");
  }
}

/* ---------------------------------------------------------------- menus */

let menuReturnFocus = null;
function showMenu(items, x, y, returnFocus, alignLeft = false) {
  closePopovers();
  ui.menu.replaceChildren(...items.map((item) => item === "-"
    ? el("hr", { class: "menu-sep" })
    : el("button", { type: "button", role: "menuitem", onclick: () => { closeMenu(); item[1](); } }, item[0])));
  ui.menu.hidden = false;
  const { innerWidth: w, innerHeight: h } = window;
  const r = ui.menu.getBoundingClientRect();
  const left = alignLeft ? x : x - r.width;
  ui.menu.style.left = `${Math.max(8, Math.min(left, w - r.width - 8))}px`;
  ui.menu.style.top = `${Math.max(8, Math.min(y, h - r.height - 8))}px`;
  menuReturnFocus = returnFocus || document.activeElement;
  ui.menu.querySelector("button")?.focus();
  syncInspoFrame();
}

function closeMenu() {
  if (ui.menu.hidden) return;
  ui.menu.hidden = true;
  ui.brand.setAttribute("aria-expanded", "false");
  menuReturnFocus?.focus?.();
  syncInspoFrame();
}

function openAssetMenu(asset, x, y, thumbEl, returnFocus) {
  const run = (action) => () => act(asset, action, action === "reveal" ? null : thumbEl);
  const items = [];
  if (asset.kind === "image") items.push(["Place in document", run("place")], ["Open as new document", run("open")]);
  else if (asset.kind === "document") items.push(["Open", run("open")], ["Place as smart object", run("place")]);
  else if (asset.kind === "font") items.push(["Install font", run("install")]);
  else if (installed().has(asset.rel)) items.push(["Import again (PhotoCraft will ask)", run("reimport")]);
  else items.push([`Import ${KIND[asset.kind].label.toLowerCase()} into PhotoCraft`, run("import")]);
  items.push(["Preview", () => openPreview(asset, returnFocus)], ["Show in Finder", run("reveal")]);
  showMenu(items, x, y, returnFocus);
}

function openBrandMenu() {
  const r = ui.brand.getBoundingClientRect();
  ui.brand.setAttribute("aria-expanded", "true");
  const sum = store.state?.store;
  showMenu([
    ...(sum?.configured ? [
      [sum.updates ? `Account & downloads (${sum.updates} update${sum.updates === 1 ? "" : "s"})` : "Account & downloads…", openStore],
      sum.signedIn ? [`Sign out${sum.email ? ` (${sum.email})` : ""}`, signOut] : ["Sign in to your store account…", openStore],
      "-"] : []),
    ["Change folder…", chooseFolder],
    ["Add pack (.zip)…", addPack],
    "-",
    ["Refresh library", refreshAll],
    ["Rebuild previews", rebuildPreviews],
    ["Reconnect PhotoCraft", () => connect("relaunch")],
    ...(NATIVE ? [["Snap to PhotoCraft", () => NATIVE.postMessage({ type: "dock" })],
                  ["Hide to floating icon", () => NATIVE.postMessage({ type: "collapse" })]] : []),
    "-",
    ["Settings…", openSettings],
  ], r.left, r.bottom + 4, ui.brand, true);
}

function renderRecent() {
  const rels = (store.state?.config.recents || []).slice(0, 15);
  const assets = rels.map((r) => store.byRel.get(r)).filter(Boolean);
  ui.recentList.replaceChildren();
  if (!assets.length) {
    ui.recentList.append(el("li", { class: "recent-empty", text: "No recent assets. Anything you place, open or import shows up here." }));
    return;
  }
  for (const a of assets) {
    const kind = KIND[a.kind];
    ui.recentList.append(el("li", {},
      el("button", { type: "button", class: "recent-item", "aria-label": `${kind.verb} ${a.name}`,
        onclick: () => { closePopovers(); act(a, PRIMARY_ACTION[kind.verb], null); } },
        el("span", { class: "recent-thumb" }, assetPicture(a, 160) ? el("img", { src: assetPicture(a, 160), alt: "", loading: "lazy",
          onload: (e) => { if (a.thumb && ALPHA_EXT.has(a.ext)) fitBackdrop(e.target, e.target.parentElement); } }) : el("span", { text: a.ext.toUpperCase() })),
        el("span", { class: "recent-text" },
          el("span", { class: "recent-name", text: displayName(a) }),
          el("span", { class: "recent-pack", text: packOf(a)?.name || "" })))));
  }
}

function toggleRecent() {
  if (!ui.recentPop.hidden) { closePopovers(); ui.recentBtn.focus(); return; }
  closeMenu();
  renderRecent();
  ui.recentPop.hidden = false;
  ui.recentBtn.setAttribute("aria-expanded", "true");
  const r = ui.recentBtn.getBoundingClientRect();
  const pr = ui.recentPop.getBoundingClientRect();
  ui.recentPop.style.top = `${r.bottom + 6}px`;
  ui.recentPop.style.left = `${Math.max(8, Math.min(r.right - pr.width, window.innerWidth - pr.width - 8))}px`;
  ui.recentList.querySelector("button")?.focus();
  syncInspoFrame();
}

function closePopovers() {
  if (!ui.recentPop.hidden) {
    ui.recentPop.hidden = true;
    ui.recentBtn.setAttribute("aria-expanded", "false");
    syncInspoFrame();
  }
}

/* --------------------------------------------------------- library ops */

async function chooseFolder() {
  if (ui.settings.open) ui.settings.close();
  toast("Choose a folder in the window that just opened…", "busy");
  try {
    const before = store.state?.config.assetsRoot;
    store.state = await api("/api/choose-folder", {});
    toast("");
    if (store.state.config.assetsRoot !== before) {
      store.expanded.clear();
      writeLocal("expanded", []);
      await loadLibrary();
      if (store.state.connection.state === "connected") toast("Folder changed. Reconnect PhotoCraft so it can read it.");
    }
    syncSettings();
    renderLink();
  } catch (err) {
    toast(err.message, "error");
  }
}

async function addPack() {
  if (ui.settings.open) ui.settings.close();
  toast("Choose one or more pack .zip files…", "busy");
  try {
    const res = await api("/api/add-pack", {});
    store.state = res.state;
    await loadLibrary();
    toast(res.message || "");
  } catch (err) {
    toast(err.message, "error");
  }
}

async function rebuildPreviews() {
  try {
    const res = await api("/api/rebuild-previews", {});
    store.state = res.state;
    rerender();
    toast(res.message);
  } catch (err) {
    toast(err.message, "error");
  }
}

async function refreshAll() {
  await Promise.all([loadLibrary(), refreshState()]);
  toast("Library refreshed");
}

function openSettings() {
  closePopovers();
  syncSettings();
  ui.settings.showModal();
  syncInspoFrame();
}



/* -------------------------------------------------------------- preview */

const pv = { list: [], index: 0, returnFocus: null };
const previewDlg = $("preview");

function previewList(asset) {
  // Browse within what's on screen: the open pack, favorites, search results or templates.
  const visible = [...ui.main.querySelectorAll(".tile-main, .card[data-rel]")].map((b) => store.byRel.get(b.dataset.rel)).filter(Boolean);
  if (visible.some((a) => a.rel === asset.rel)) return visible;
  return packOf(asset)?.assets || [asset];
}

function openPreview(asset, returnFocus) {
  pv.list = previewList(asset);
  pv.index = Math.max(0, pv.list.findIndex((a) => a.rel === asset.rel));
  pv.returnFocus = returnFocus || document.activeElement;
  renderPreview();
  if (!previewDlg.open) previewDlg.showModal();
  syncInspoFrame();
  $("preview-primary").focus();
}

function renderPreview() {
  const a = pv.list[pv.index];
  if (!a) return;
  const kind = KIND[a.kind];
  $("preview-name").textContent = displayName(a);
  $("preview-meta").textContent = [packOf(a)?.name, kind.label, a.ext.toUpperCase(), pv.list.length > 1 ? `${pv.index + 1} of ${pv.list.length}` : ""]
    .filter(Boolean).join(", ");
  const img = $("preview-img");
  const src = a.thumb ? thumbUrl(a.rel, PREVIEW_PX, "fit") : assetPicture(a, PREVIEW_PX);
  img.hidden = !src;
  if (src) {
    img.src = src;
    img.alt = `Preview of ${displayName(a)}`;
    const frame = img.parentElement;
    frame.classList.remove("is-dark-art", "is-light-art");
    if (a.thumb && ALPHA_EXT.has(a.ext)) fitBackdrop(img, frame);
  }
  $("preview-primary").textContent = installed().has(a.rel) ? "Already in PhotoCraft" : `${kind.verb} in PhotoCraft`;
  $("preview-primary").disabled = installed().has(a.rel);
  const fav = favorites().has(a.rel);
  $("preview-fav").setAttribute("aria-pressed", String(fav));
  $("preview-fav").querySelector("span").textContent = fav ? "Favorited" : "Favorite";
  $("preview-prev").disabled = pv.index === 0;
  $("preview-next").disabled = pv.index >= pv.list.length - 1;
  // warm the neighbours so arrowing feels instant
  for (const n of [pv.list[pv.index + 1], pv.list[pv.index - 1]]) if (n?.thumb) new Image().src = thumbUrl(n.rel, PREVIEW_PX, "fit");
}

function stepPreview(d) {
  const next = pv.index + d;
  if (next < 0 || next >= pv.list.length) return;
  pv.index = next;
  renderPreview();
}

$("preview-prev").addEventListener("click", () => stepPreview(-1));
$("preview-next").addEventListener("click", () => stepPreview(1));
$("preview-close").addEventListener("click", () => previewDlg.close());
$("preview-primary").addEventListener("click", () => {
  const a = pv.list[pv.index];
  previewDlg.close();
  act(a, PRIMARY_ACTION[KIND[a.kind].verb], null);
});
$("preview-fav").addEventListener("click", async () => {
  const a = pv.list[pv.index];
  const on = !favorites().has(a.rel);
  try {
    store.state = await api("/api/favorite", { rel: a.rel, on });
    renderPreview();
    rerender();
  } catch (err) { toast(err.message, "error"); }
});
previewDlg.addEventListener("keydown", (e) => {
  if (e.key === "ArrowRight") { e.preventDefault(); stepPreview(1); }
  else if (e.key === "ArrowLeft") { e.preventDefault(); stepPreview(-1); }
  else if (e.key === " ") { e.preventDefault(); previewDlg.close(); }
  else if ((e.key === "f" || e.key === "F") && !e.metaKey) { e.preventDefault(); $("preview-fav").click(); }
});
previewDlg.addEventListener("click", (e) => { if (e.target === previewDlg) previewDlg.close(); });
previewDlg.addEventListener("close", () => { syncInspoFrame(); pv.returnFocus?.focus?.(); });

/* ---------------------------------------------------------------- store */

let shopInFlight = false;
async function refreshShop() {
  if (!store.state?.store?.configured) { store.shop = null; return; }
  if (shopInFlight) return;
  shopInFlight = true;
  try {
    const before = store.shop;
    store.shop = await api("/api/store");
    if (store.shopCompleted !== null && store.shop.completed > store.shopCompleted) await loadLibrary();
    store.shopCompleted = store.shop.completed;
    renderShopBar();
    if (ui.storeDlg.open) renderStoreDialog();
    const changed = !before || JSON.stringify(before.items.map((i) => [i.id, i.status])) !== JSON.stringify(store.shop.items.map((i) => [i.id, i.status]));
    const cur = store.shop.current;
    if (store.tab !== "inspo" && ui.menu.hidden && !store.busy) {
      if (changed) rerender();
      else if (cur) updateStripProgress(cur);
    }
  } catch { /* panel server busy; next poll retries */ } finally {
    shopInFlight = false;
  }
}

function updateStripProgress(cur) {
  const pct = cur.total ? Math.min(100, Math.round((cur.done / cur.total) * 100)) : 0;
  for (const strip of ui.main.querySelectorAll(".card.is-loading, .row.is-loading")) {
    strip.querySelector(".card-state")?.replaceChildren(strip.classList.contains("row") ? `${pct}%` : `Downloading ${pct}%`);
    const fill = strip.querySelector(".strip-fill");
    if (fill) fill.style.width = `${pct}%`;
  }
}

function renderShopBar() {
  const sum = store.shop || store.state?.store;
  if (!sum) return;
  ui.storeDot.hidden = !(sum.updates > 0 || sum.current);
  try { NATIVE?.postMessage({ type: "badge", on: !ui.storeDot.hidden }); } catch { /* no native bridge */ }
  const cur = sum.current;
  if (cur) {
    const pct = cur.total ? Math.min(100, Math.round((cur.done / cur.total) * 100)) : 0;
    const more = (sum.queue || []).length;
    ui.dlText.textContent = `Downloading ${cur.name}, ${pct}%${more ? ` (${more} more queued)` : ""}`;
    ui.dlFill.style.width = `${pct}%`;
    ui.dlBar.hidden = false;
  } else if (sum.pausedUntil && (sum.queue || []).length) {
    const at = new Date(sum.pausedUntil * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
    ui.dlText.textContent = `Store download limit reached. ${sum.queue.length} waiting, next try around ${at}.`;
    ui.dlFill.style.width = "0%";
    ui.dlBar.hidden = false;
  } else {
    ui.dlBar.hidden = true;
  }
  syncInspoFrame();
}

async function shopDownload(ids, all) {
  try {
    const res = await api("/api/store/download", all ? { all } : { ids });
    store.shop = res.store;
    renderShopBar();
    rerender();
    if (ui.storeDlg.open) renderStoreDialog();
    toast(res.message);
  } catch (err) {
    toast(err.message, "error");
  }
}

function openStore() {
  closePopovers();
  closeMenu();
  renderStoreDialog();
  ui.storeDlg.showModal();
  syncInspoFrame();
  refreshShop();
}

const show = (id, on) => { $(id).hidden = !on; };
function formError(id, message) {
  const n = $(id);
  n.textContent = message || "";
  n.hidden = !message;
}

let signinStep = "email";
function renderStoreDialog() {
  const sh = store.shop || store.state?.store || {};
  const configured = !!sh.configured;
  show("store-unconfigured", !configured);
  const signedIn = configured && sh.signedIn;
  show("signin-email", configured && !signedIn && signinStep === "email");
  show("signin-code", configured && !signedIn && signinStep === "code");
  show("signin-order", configured && !signedIn && signinStep === "order");
  show("store-account", signedIn);
  if (!signedIn) {
    if (!$("store-email").value) $("store-email").value = store.signinEmail || sh.email || "";
    if (!$("order-email").value) $("order-email").value = store.signinEmail || sh.email || "";
    return;
  }
  $("account-email").textContent = sh.email || "Signed in";
  const last = sh.lastCheck ? new Date(sh.lastCheck * 1000).toLocaleString([], { dateStyle: "medium", timeStyle: "short" }) : "not yet";
  const parts = [`Last checked ${last}.`];
  if (sh.updates) parts.push(`${sh.updates} update${sh.updates === 1 ? "" : "s"} ready.`);
  if (sh.missing) parts.push(`${sh.missing} not downloaded.`);
  if (sh.message) parts.push(sh.message);
  $("account-status").textContent = parts.join(" ");
  $("auto-update").checked = !!sh.autoUpdate;
  $("auto-activate").checked = !!sh.autoActivate;
  const toGet = (sh.items || []).filter((i) => i.ready && ["missing", "failed", "update"].includes(i.status)).length;
  $("get-all").disabled = toGet === 0;
  $("get-all").classList.toggle("btn-primary", toGet > 0);
  $("get-all").textContent = toGet ? `Download all (${toGet})` : "Everything is downloaded";
  const items = sh.items || [];
  ui.storeList.replaceChildren(...items.map((it) => {
    const label = { installed: "Installed", update: "Update ready", missing: "Not downloaded", queued: "Queued",
      downloading: "Downloading", failed: "Failed" }[it.status] || it.status;
    const cur = sh.current && sh.current.id === it.id ? sh.current : null;
    const pct = cur && cur.total ? Math.round((cur.done / cur.total) * 100) : null;
    const btn = it.status === "update" ? el("button", { class: "btn btn-small btn-primary", type: "button", onclick: () => shopDownload([it.id]) }, "Update")
      : (it.status === "missing" || it.status === "failed") && it.ready ? el("button", { class: "btn btn-small", type: "button", onclick: () => shopDownload([it.id]) }, it.status === "failed" ? "Retry" : "Download")
      : null;
    const kindLabel = { pack: "Pack", brushes: "Brushes", style: "Layer styles", template: "Template" }[it.kind] || "";
    return el("li", { class: "store-row" },
      it.hasCover ? el("img", { class: "store-row-cover", src: coverUrl(it.id), alt: "", loading: "lazy" }) : el("span", { class: "store-row-cover" }),
      el("span", { class: "store-row-text" },
        el("span", { class: "store-row-name", text: it.name }),
        el("span", { class: "store-row-meta", text: [kindLabel, it.files ? `${it.files} files` : "", formatBytes(it.bytes)].filter(Boolean).join(", ") }),
        it.error ? el("span", { class: "store-row-error", text: it.error }) : null),
      el("span", { class: `chip is-${it.status}`, text: pct !== null ? `${pct}%` : label }),
      btn);
  }));
}

async function storePost(op, body, errId) {
  if (errId) formError(errId, "");
  const res = await api(`/api/store/${op}`, body).catch((err) => { if (errId) formError(errId, err.message); else toast(err.message, "error"); return null; });
  if (!res) return null;
  store.shop = res.store;
  if (store.state) store.state.store = { ...store.state.store, ...res.store };
  renderShopBar();
  renderStoreDialog();
  return res;
}

$("signin-email").addEventListener("submit", async (e) => {
  e.preventDefault();
  const email = $("store-email").value.trim();
  store.signinEmail = email;
  const btn = $("send-code"); btn.disabled = true;
  const res = await storePost("start", { email }, "signin-error");
  btn.disabled = false;
  if (res) {
    signinStep = "code";
    $("code-hint").textContent = `We sent a code to ${email}. It expires in 10 minutes.`;
    renderStoreDialog();
    $("store-code").value = "";
    $("store-code").focus();
  }
});
$("signin-code").addEventListener("submit", async (e) => {
  e.preventDefault();
  const btn = $("verify-code"); btn.disabled = true;
  const res = await storePost("verify", { email: store.signinEmail, code: $("store-code").value }, "code-error");
  btn.disabled = false;
  if (res) { signinStep = "email"; toast("Signed in to your store account"); renderStoreDialog(); rerender(); }
});
$("store-code").addEventListener("input", () => {
  const v = $("store-code").value.replace(/\D/g, "").slice(0, 6);
  $("store-code").value = v;
  if (v.length === 6) $("signin-code").requestSubmit();
});
$("signin-order").addEventListener("submit", async (e) => {
  e.preventDefault();
  const email = $("order-email").value.trim();
  store.signinEmail = email;
  const res = await storePost("order", { email, orderNumber: $("order-number").value }, "order-error");
  if (res) { signinStep = "email"; toast("Signed in to your store account"); renderStoreDialog(); rerender(); }
});
$("resend-code").addEventListener("click", async () => {
  const res = await storePost("start", { email: store.signinEmail }, "code-error");
  if (res) toast(res.message);
});
$("change-email").addEventListener("click", () => { signinStep = "email"; renderStoreDialog(); $("store-email").focus(); });
$("use-order").addEventListener("click", () => { signinStep = "order"; renderStoreDialog(); $("order-number").focus(); });
$("use-email").addEventListener("click", () => { signinStep = "email"; renderStoreDialog(); $("store-email").focus(); });
async function signOut() {
  const r = await storePost("signout", {});
  if (r) { signinStep = "email"; toast(r.message); rerender(); }
}
$("sign-out").addEventListener("click", signOut);
$("check-now").addEventListener("click", async () => {
  const btn = $("check-now"); btn.disabled = true;
  const r = await storePost("check", {});
  btn.disabled = false;
  if (r) { toast(r.message); rerender(); }
});
$("get-all").addEventListener("click", () => shopDownload(null, "everything"));
$("activate-all").addEventListener("click", async () => {
  const btn = $("activate-all"); btn.disabled = true;
  toast("Adding brushes and layer styles to PhotoCraft…", "busy");
  const r = await storePost("activate", {});
  btn.disabled = false;
  if (r) { toast(r.message); await refreshState(); rerender(); }
});
$("auto-activate").addEventListener("change", async () => {
  const r = await storePost("auto-activate", { on: $("auto-activate").checked });
  if (r) toast(r.message);
});
$("auto-update").addEventListener("change", async () => {
  const r = await storePost("auto", { on: $("auto-update").checked });
  if (r) toast(r.message);
});
$("store-close").addEventListener("click", () => ui.storeDlg.close());
ui.storeDlg.addEventListener("close", syncInspoFrame);

/* ---------------------------------------------------------------- data */

async function loadLibrary() {
  try {
    store.library = await api("/api/library");
    store.libraryError = "";
  } catch (err) {
    store.library = store.library || { packs: [] };
    store.libraryError = err.message;
  }
  store.byRel = new Map();
  store.packOfRel = new Map();
  for (const p of store.library.packs) for (const a of p.assets) { store.byRel.set(a.rel, a); store.packOfRel.set(a.rel, p); }
  rerender();
}

function balanceScrollbar() {
  const sbw = Math.max(0, ui.main.offsetWidth - ui.main.clientWidth);
  ui.main.style.setProperty("--sbw", `${sbw}px`);
}
new ResizeObserver(balanceScrollbar).observe(ui.main);

function rerender() {
  const top = ui.main.scrollTop;
  render();
  ui.main.scrollTop = top;
  balanceScrollbar();
}

let stateInFlight = false;
async function refreshState() {
  if (stateInFlight) return;
  stateInFlight = true;
  try {
    const before = installedKey();
    store.state = await api("/api/state");
    renderLink();
    if (!store.shop) renderShopBar();
    if (store.library && installedKey() !== before && ui.menu.hidden && !store.busy && store.tab !== "inspo") rerender();
  } catch (err) {
    ui.linkBar.hidden = false;
    ui.linkText.textContent = err.message;
    ui.linkAction.hidden = true;
    ui.linkDot.dataset.state = "off";
    syncInspoFrame();
  } finally {
    stateInFlight = false;
  }
}

function syncSettings() {
  const cfg = store.state?.config;
  if (!cfg) return;
  ui.rootPath.textContent = cfg.assetsRoot || "Not chosen yet";
  ui.fitToggle.checked = !!cfg.fitOnPlace;
  if (document.activeElement !== ui.inspoUrl) ui.inspoUrl.value = cfg.inspoUrl || "";
  if (document.activeElement !== ui.storeUrl) ui.storeUrl.value = store.state.store?.storeUrl || "";
  ui.version.textContent = `Pack Panel ${store.state.version} · PhotoCraft link on port ${store.state.controlPort}`;
}

// The native app sends a picture of the Inspo page to show while a menu covers it.
window.packPanelInspoSnapshot = (src) => {
  const host = document.getElementById("inspo-host");
  if (host && store.tab === "inspo") host.replaceChildren(el("img", { class: "inspo-snap", src, alt: "" }));
};

/* -------------------------------------------------------------- events */

ui.linkAction.addEventListener("click", () => connect(ui.linkAction.dataset.op));
ui.brand.addEventListener("click", () => (ui.menu.hidden ? openBrandMenu() : closeMenu()));
ui.favFilter.addEventListener("click", () => {
  store.favOnly = !store.favOnly;
  if (store.tab !== "packs") store.tab = "packs";
  render();
  ui.main.scrollTop = 0;
});
ui.recentBtn.addEventListener("click", toggleRecent);
$("open-settings").addEventListener("click", openSettings);
$("collapse-btn").addEventListener("click", () => NATIVE?.postMessage({ type: "collapse" }));
$("choose-folder").addEventListener("click", chooseFolder);
$("add-pack-settings").addEventListener("click", addPack);
$("relaunch").addEventListener("click", () => { ui.settings.close(); connect("relaunch"); });
ui.settings.addEventListener("close", syncInspoFrame);
ui.fitToggle.addEventListener("change", async () => {
  try { store.state = await api("/api/config", { fitOnPlace: ui.fitToggle.checked }); }
  catch (err) { ui.fitToggle.checked = !ui.fitToggle.checked; toast(err.message, "error"); }
});
ui.storeUrl.addEventListener("change", async () => {
  try {
    store.state = await api("/api/config", { storeUrl: ui.storeUrl.value.trim() });
    toast(store.state.store?.configured ? "Store address saved" : "Store address removed");
    await refreshShop();
    rerender();
  } catch (err) {
    toast(err.message, "error");
  }
});
ui.inspoUrl.addEventListener("change", async () => {
  try {
    store.state = await api("/api/config", { inspoUrl: ui.inspoUrl.value.trim() });
    toast(store.state.config.inspoUrl ? "Inspo page saved" : "Inspo page removed");
    if (store.tab === "inspo") render();
  } catch (err) {
    toast(err.message, "error");
  }
});

for (const tab of ui.tabs) {
  tab.addEventListener("click", () => {
    if (store.tab === tab.dataset.tab) return;
    store.tab = tab.dataset.tab;
    store.packId = null;
    writeLocal("tab", store.tab);
    if (store.tab !== "packs") store.favOnly = false;
    render();
    ui.main.scrollTop = 0;
  });
}

let searchTimer;
ui.search.addEventListener("input", () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => { store.query = ui.search.value.trim(); render(); ui.main.scrollTop = 0; }, 120);
});

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") {
    if (!ui.menu.hidden) { closeMenu(); return; }
    if (!ui.recentPop.hidden) { closePopovers(); ui.recentBtn.focus(); return; }
    if (ui.settings.open || ui.storeDlg.open || $("preview").open || $("confirm").open) return;  // the dialog handles its own Escape
    if (store.query) { ui.search.value = ""; store.query = ""; render(); return; }
    if (store.favOnly) { store.favOnly = false; render(); return; }
    if (store.openPack) { togglePack(store.openPack); return; }
  }
  if (e.key === "/" && document.activeElement !== ui.search && !ui.settings.open && !ui.storeDlg.open && !$("preview").open && store.tab !== "inspo"
      && !(document.activeElement instanceof HTMLInputElement)) {
    e.preventDefault();
    ui.search.focus();
  }
  if (!ui.menu.hidden && (e.key === "ArrowDown" || e.key === "ArrowUp")) {
    e.preventDefault();
    const items = [...ui.menu.querySelectorAll("button")];
    const i = items.indexOf(document.activeElement);
    items[(i + (e.key === "ArrowDown" ? 1 : -1) + items.length) % items.length].focus();
  }
});
document.addEventListener("pointerdown", (e) => {
  if (!ui.menu.hidden && !ui.menu.contains(e.target) && !ui.brand.contains(e.target)) closeMenu();
  if (!ui.recentPop.hidden && !ui.recentPop.contains(e.target) && !ui.recentBtn.contains(e.target)) closePopovers();
});
ui.main.addEventListener("scroll", () => { if (!ui.menu.hidden) closeMenu(); }, { passive: true });
window.addEventListener("resize", () => { closeMenu(); closePopovers(); syncInspoFrame(); });

/* ---------------------------------------------------------------- boot */

(async function boot() {
  document.documentElement.classList.toggle("is-native", !!NATIVE);
  const startTab = location.hash.slice(1) || readLocal("tab", "packs");
  if (["packs", "templates", "inspo"].includes(startTab)) store.tab = startTab;
  render();
  await refreshState();
  syncSettings();
  await refreshShop();
  await loadLibrary();
  setInterval(refreshState, 5000);
  setInterval(() => {
    const sum = store.shop || store.state?.store;
    const waitingForList = sum?.signedIn && !(store.shop?.items || []).length;  // right after launch or sign-in
    if (ui.storeDlg.open || sum?.current || (sum?.queue || []).length || waitingForList) refreshShop();
  }, 1500);
  setInterval(refreshShop, 60000);
})();
