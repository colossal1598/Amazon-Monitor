const AUTH_KEY = "admin_ui_auth";
const ACTIVE_TAB_KEY = "admin_ui_tab";

const state = {
  sqlitePollId: null,
  healthPollId: null,
  bulkRole: null,
  toastTimer: null,
  savedSettings: null, // snapshot of last loaded/saved form values (JSON string)
  watchItems: [],
};

const ENGINE_STATUS_HE = {
  starting: "מתחיל",
  running: "פועל",
  idle_no_watch: "אין מוצרים במעקב",
  captcha_pause: "מושהה — CAPTCHA",
};

function byId(id) {
  return document.getElementById(id);
}

function toLines(text) {
  return String(text || "")
    .split(/\r?\n/)
    .map((v) => v.trim())
    .filter(Boolean);
}

function isValidAsin(raw) {
  return /^[A-Z0-9]{10}$/.test(String(raw || "").trim().toUpperCase());
}

function userFacingError(payload, status) {
  if (payload && payload.message && /[\u0590-\u05FF]/.test(String(payload.message))) {
    return String(payload.message);
  }
  const code = payload && (payload.error || payload.message);
  if (code === "cooldown" || code === "pm2_not_found") {
    return payload.message || "לא ניתן להפעיל מחדש כעת";
  }
  if (status === 404) {
    return "הפעולה לא נמצאה";
  }
  if (status >= 500) {
    return "שגיאת שרת — נסו שוב";
  }
  return "הפעולה נכשלה";
}

/* ===== Auth ===== */

function getAuthHeader() {
  const token = sessionStorage.getItem(AUTH_KEY);
  return token ? `Basic ${token}` : null;
}

function setAuth(user, pass) {
  sessionStorage.setItem(AUTH_KEY, btoa(`${user}:${pass}`));
}

function clearAuth() {
  sessionStorage.removeItem(AUTH_KEY);
}

function showLogin(errMsg) {
  byId("app-shell").classList.add("hidden");
  byId("login-screen").classList.remove("hidden");
  const err = byId("login-error");
  if (errMsg) {
    err.textContent = errMsg;
    err.classList.remove("hidden");
  } else {
    err.classList.add("hidden");
  }
}

function showApp() {
  byId("login-screen").classList.add("hidden");
  byId("app-shell").classList.remove("hidden");
}

/* ===== Toast ===== */

function showToast(message, isError = false) {
  const toast = byId("toast");
  toast.textContent = message;
  toast.classList.toggle("error", !!isError);
  toast.classList.remove("hidden");
  if (state.toastTimer) {
    window.clearTimeout(state.toastTimer);
  }
  state.toastTimer = window.setTimeout(() => toast.classList.add("hidden"), 4000);
}

function isAuthError(err) {
  return err.message === "unauthorized" || err.message === "not_logged_in";
}

/* ===== API ===== */

async function apiJson(url, options = {}) {
  const auth = getAuthHeader();
  if (!auth) {
    showLogin("יש להתחבר קודם");
    throw new Error("not_logged_in");
  }

  const opts = { ...options };
  const headers = { ...(opts.headers || {}), Authorization: auth };
  if (opts.body && !headers["Content-Type"]) {
    headers["Content-Type"] = "application/json; charset=utf-8";
  }
  opts.headers = headers;

  const res = await fetch(url, opts);
  let payload = {};
  try {
    payload = await res.json();
  } catch (_err) {
    payload = {};
  }

  if (res.status === 401) {
    clearAuth();
    showLogin("שם משתמש או סיסמה שגויים");
    throw new Error("unauthorized");
  }
  if (!res.ok) {
    throw new Error(userFacingError(payload, res.status));
  }
  return payload;
}

/* ===== Tabs ===== */

function switchTab(tabId) {
  document.querySelectorAll(".tab-panel").forEach((panel) => {
    panel.classList.toggle("active", panel.id === `tab-${tabId}`);
  });
  document.querySelectorAll(".nav-item[data-tab]").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.tab === tabId);
  });
  sessionStorage.setItem(ACTIVE_TAB_KEY, tabId);
}

function restoreTab() {
  const saved = sessionStorage.getItem(ACTIVE_TAB_KEY);
  if (saved && byId(`tab-${saved}`)) {
    switchTab(saved);
  }
}

/* ===== ASIN lists ===== */

function updateAsinCounts(role, count) {
  if (role === "watch") {
    byId("nav-watch-count").textContent = count > 0 ? String(count) : "";
    byId("ov-watch-count").textContent = String(count);
  } else {
    byId("ov-blacklist-count").textContent = String(count);
  }
}

function createTargetPriceInput(item) {
  const input = document.createElement("input");
  input.type = "number";
  input.step = "0.01";
  input.min = "0.01";
  input.dir = "ltr";
  input.className = "target-price-input";
  input.placeholder = "—";
  input.value = item.target_price != null ? item.target_price : "";

  const flash = (className) => {
    input.classList.remove("target-saved", "target-error");
    input.classList.add(className);
    window.setTimeout(() => input.classList.remove(className), 1400);
  };

  input.addEventListener("change", async () => {
    const raw = String(input.value || "").trim();
    let targetPrice = null;
    if (raw !== "") {
      const n = parseFloat(raw);
      if (!Number.isFinite(n) || n <= 0) {
        flash("target-error");
        showToast("מחיר יעד לא תקין — יש להזין מספר גדול מ־0", true);
        return;
      }
      targetPrice = Math.round(n * 100) / 100;
    }
    try {
      await apiJson("/api/asins/target", {
        method: "PUT",
        body: JSON.stringify({ asin: item.asin, target_price: targetPrice }),
      });
      input.value = targetPrice != null ? targetPrice : "";
      flash("target-saved");
    } catch (err) {
      flash("target-error");
      if (!isAuthError(err)) {
        showToast(`שגיאה בשמירת מחיר יעד: ${err.message}`, true);
      }
    }
  });
  input.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter") {
      ev.preventDefault();
      input.blur();
    }
  });

  return input;
}

function renderAsinTable(role, items) {
  if (role === "watch") {
    state.watchItems = items;
    renderFreshnessTable(state.lastHealth);
  }
  const tbody = byId(role === "watch" ? "watch-body" : "blacklist-body");
  const emptyNote = byId(role === "watch" ? "watch-empty" : "blacklist-empty");
  tbody.innerHTML = "";
  emptyNote.classList.toggle("hidden", items.length > 0);
  updateAsinCounts(role, items.length);

  for (const item of items) {
    const tr = document.createElement("tr");

    const asinTd = document.createElement("td");
    const code = document.createElement("code");
    code.textContent = item.asin;
    asinTd.appendChild(code);
    tr.appendChild(asinTd);

    const notesTd = document.createElement("td");
    notesTd.textContent = item.notes || "";
    tr.appendChild(notesTd);

    if (role === "watch") {
      const targetTd = document.createElement("td");
      targetTd.className = "td-target";
      targetTd.appendChild(createTargetPriceInput(item));
      tr.appendChild(targetTd);
    }

    const actionTd = document.createElement("td");
    const removeBtn = document.createElement("button");
    removeBtn.type = "button";
    removeBtn.className = "btn-remove";
    removeBtn.textContent = "הסרה";
    removeBtn.addEventListener("click", async () => {
      try {
        await apiJson(`/api/asins/${encodeURIComponent(item.asin)}/${role}`, { method: "DELETE" });
        showToast(`הקוד ${item.asin} הוסר`);
        await loadAsins(role);
      } catch (err) {
        if (!isAuthError(err)) {
          showToast(`שגיאה בהסרה: ${err.message}`, true);
        }
      }
    });
    actionTd.appendChild(removeBtn);
    tr.appendChild(actionTd);

    tbody.appendChild(tr);
  }
}

async function loadAsins(role) {
  const payload = await apiJson(`/api/asins?role=${encodeURIComponent(role)}`);
  renderAsinTable(role, payload.items || []);
}

async function addAsin(role) {
  const asinInput = byId(role === "watch" ? "watch-asin-input" : "blacklist-asin-input");
  const notesInput = byId(role === "watch" ? "watch-notes-input" : "blacklist-notes-input");
  const asin = String(asinInput.value || "").trim().toUpperCase();
  const notes = String(notesInput.value || "").trim();
  if (!isValidAsin(asin)) {
    showToast("יש להזין קוד מוצר תקין — 10 אותיות/ספרות באנגלית", true);
    asinInput.focus();
    return;
  }
  try {
    await apiJson("/api/asins", {
      method: "POST",
      body: JSON.stringify({ asin, role, notes }),
    });
    asinInput.value = "";
    notesInput.value = "";
    showToast(`הקוד ${asin} נוסף`);
    await loadAsins(role);
  } catch (err) {
    if (!isAuthError(err)) {
      showToast(`שגיאה בהוספה: ${err.message}`, true);
    }
  }
}

/* ===== Bulk paste ===== */

function openBulkModal(role) {
  state.bulkRole = role;
  byId("bulk-modal-title").textContent =
    role === "watch" ? "הדבקת רשימה — מוצרים במעקב" : "הדבקת רשימה — רשימה שחורה";
  byId("bulk-text").value = "";
  byId("bulk-modal").classList.remove("hidden");
  byId("bulk-text").focus();
}

function closeBulkModal() {
  byId("bulk-modal").classList.add("hidden");
  state.bulkRole = null;
}

async function applyBulk() {
  const role = state.bulkRole;
  if (!role) {
    return;
  }
  const parts = byId("bulk-text").value
    .split(/[^A-Za-z0-9]+/g)
    .map((x) => x.trim().toUpperCase())
    .filter(Boolean);
  const seen = new Set();
  const valid = [];
  for (const value of parts) {
    if (!seen.has(value) && isValidAsin(value)) {
      seen.add(value);
      valid.push(value);
    }
  }
  if (!valid.length) {
    showToast("לא נמצאו קודי מוצר תקינים ברשימה", true);
    return;
  }

  let success = 0;
  for (const asin of valid) {
    try {
      await apiJson("/api/asins", {
        method: "POST",
        body: JSON.stringify({ asin, role }),
      });
      success += 1;
    } catch (_err) {
      // continue with the rest
    }
  }
  closeBulkModal();
  await loadAsins(role);
  showToast(`נוספו ${success} מתוך ${valid.length} מוצרים`);
}

/* ===== Settings form ===== */

function parsePositiveInt(raw, fallback) {
  const n = parseInt(String(raw || "").trim(), 10);
  return Number.isFinite(n) && n > 0 ? n : fallback;
}

function parsePercent(raw, fallback) {
  const n = parseInt(String(raw || "").trim(), 10);
  if (!Number.isFinite(n)) {
    return fallback;
  }
  return Math.min(99, Math.max(1, n));
}

function fillSettings(settings) {
  byId("asin-check-interval-seconds").value = settings.asin_check_interval_seconds ?? "";
  byId("stream-concurrent-tabs").value = settings.stream_concurrent_tabs ?? "";
  byId("aes-check-minutes").value = settings.aes_check_minutes ?? "";
  byId("browser-recycle-minutes").value = settings.browser_recycle_minutes ?? "";
  byId("pdp-settle-seconds").value = settings.pdp_settle_seconds ?? "";
  byId("pdp-continue-shopping-max-clicks").value =
    settings.pdp_continue_shopping_max_clicks ?? "";
  byId("playwright-headless").checked = settings.playwright_headless !== false;

  byId("wa-group-id").value = settings.wa_group_id || "";
  byId("wa-client-to").value = settings.wa_client_to || "";

  byId("price-drop-percent").value = settings.price_drop_percent ?? "";
  byId("max-requests-per-minute").value = settings.max_requests_per_minute ?? "";
  byId("affiliate-tag").value = settings.affiliate_tag || "";

  byId("aes-url").value = ((settings.search_urls || {}).aes_llc) || "";
  byId("required-keywords").value = (settings.required_keywords || []).join("\n");
  byId("title-blacklist-phrases").value = (settings.title_blacklist_phrases || []).join("\n");
  byId("allowed-sellers").value = (settings.pdp_allowed_seller_substrings || []).join("\n");

  const templates = settings.wa_message_templates || {};
  byId("tpl-new-product").value = templates.new_product || "";
  byId("tpl-price-drop").value = templates.price_drop || "";
  byId("tpl-back-in-stock").value = templates.back_in_stock || "";
  byId("tpl-price-below-target").value = templates.price_below_target || "";
}

function collectSettingsPayload() {
  const streamTabs = parsePositiveInt(byId("stream-concurrent-tabs").value, 2);
  return {
    asin_check_interval_seconds: (() => {
      const n = parseInt(String(byId("asin-check-interval-seconds").value || "").trim(), 10);
      return Number.isFinite(n) ? Math.max(15, n) : 60;
    })(),
    stream_concurrent_tabs: Math.min(4, Math.max(1, streamTabs)),
    aes_check_minutes: parsePositiveInt(byId("aes-check-minutes").value, 5),
    browser_recycle_minutes: parsePositiveInt(byId("browser-recycle-minutes").value, 60),
    pdp_settle_seconds: (() => {
      const n = parseFloat(String(byId("pdp-settle-seconds").value || "").trim());
      return Number.isFinite(n) ? Math.min(30, Math.max(1, n)) : 8.0;
    })(),
    pdp_continue_shopping_max_clicks: (() => {
      const n = parseInt(
        String(byId("pdp-continue-shopping-max-clicks").value || "").trim(),
        10,
      );
      return Number.isFinite(n) ? Math.min(10, Math.max(0, n)) : 3;
    })(),
    playwright_headless: byId("playwright-headless").checked,
    wa_group_id: String(byId("wa-group-id").value || "").trim(),
    wa_client_to: String(byId("wa-client-to").value || "").trim(),
    price_drop_percent: parsePercent(byId("price-drop-percent").value, 10),
    max_requests_per_minute: parsePositiveInt(byId("max-requests-per-minute").value, 25),
    affiliate_tag: String(byId("affiliate-tag").value || "").trim(),
    search_urls: {
      aes_llc: String(byId("aes-url").value || "").trim(),
    },
    required_keywords: toLines(byId("required-keywords").value),
    title_blacklist_phrases: toLines(byId("title-blacklist-phrases").value),
    pdp_allowed_seller_substrings: toLines(byId("allowed-sellers").value),
    wa_message_templates: {
      new_product: byId("tpl-new-product").value,
      price_drop: byId("tpl-price-drop").value,
      back_in_stock: byId("tpl-back-in-stock").value,
      price_below_target: byId("tpl-price-below-target").value,
    },
  };
}

/* ===== Dirty tracking / save bar ===== */

function snapshotForm() {
  state.savedSettings = JSON.stringify(collectSettingsPayload());
  updateSaveBar();
}

function isDirty() {
  if (state.savedSettings == null) {
    return false;
  }
  return JSON.stringify(collectSettingsPayload()) !== state.savedSettings;
}

function updateSaveBar() {
  byId("save-bar").classList.toggle("hidden", !isDirty());
}

function bindDirtyTracking() {
  const ids = [
    "asin-check-interval-seconds", "stream-concurrent-tabs", "aes-check-minutes",
    "browser-recycle-minutes",
    "pdp-settle-seconds", "pdp-continue-shopping-max-clicks", "playwright-headless",
    "wa-group-id", "wa-client-to", "price-drop-percent", "max-requests-per-minute",
    "affiliate-tag", "aes-url", "required-keywords", "title-blacklist-phrases",
    "allowed-sellers", "tpl-new-product", "tpl-price-drop", "tpl-back-in-stock",
    "tpl-price-below-target",
  ];
  for (const id of ids) {
    const el = byId(id);
    el.addEventListener("input", updateSaveBar);
    el.addEventListener("change", updateSaveBar);
  }
}

async function loadSettings() {
  const payload = await apiJson("/api/settings");
  fillSettings(payload.settings || {});
  snapshotForm();
}

async function saveSettings() {
  const button = byId("save-btn");
  button.disabled = true;
  button.textContent = "שומר...";
  try {
    const settings = collectSettingsPayload();
    await apiJson("/api/settings", {
      method: "PUT",
      body: JSON.stringify({ settings }),
    });
    snapshotForm();
    showToast("ההגדרות נשמרו בהצלחה");
  } catch (err) {
    if (!isAuthError(err)) {
      showToast(`השמירה נכשלה: ${err.message}`, true);
    }
  } finally {
    button.disabled = false;
    button.textContent = "שמירה";
  }
}

async function discardChanges() {
  try {
    await loadSettings();
    showToast("השינויים בוטלו");
  } catch (err) {
    if (!isAuthError(err)) {
      showToast("לא ניתן לטעון מחדש את ההגדרות", true);
    }
  }
}

/* ===== Health / engine overview ===== */

function formatAgo(isoString) {
  if (!isoString) {
    return "מעולם לא נבדק";
  }
  const then = new Date(isoString);
  if (Number.isNaN(then.getTime())) {
    return "—";
  }
  const sec = Math.floor((Date.now() - then.getTime()) / 1000);
  if (sec < 5) {
    return "עכשיו";
  }
  if (sec < 60) {
    return `לפני ${sec} שניות`;
  }
  const min = Math.floor(sec / 60);
  if (min < 60) {
    return `לפני ${min} דקות`;
  }
  const hours = Math.floor(min / 60);
  return `לפני ${hours} שעות`;
}

function secondsSince(isoString) {
  if (!isoString) {
    return null;
  }
  const then = new Date(isoString);
  if (Number.isNaN(then.getTime())) {
    return null;
  }
  return Math.floor((Date.now() - then.getTime()) / 1000);
}

function formatResultHe(info) {
  const result = info && info.result;
  if (result === "ok") {
    if (info.in_stock === true) {
      const price = info.price;
      if (price != null && Number.isFinite(Number(price))) {
        return `במלאי · $${Number(price).toFixed(2)}`;
      }
      return "במלאי";
    }
    if (info.in_stock === false) {
      return "אזל מהמלאי";
    }
    return "תקין";
  }
  if (!result) {
    return "—";
  }
  if (result === "captcha") {
    return "CAPTCHA";
  }
  if (result === "no_row") {
    return "ללא נתונים";
  }
  if (String(result).startsWith("skip:")) {
    return `דילוג: ${String(result).slice(5)}`;
  }
  return String(result);
}

function engineStatusHe(status) {
  return ENGINE_STATUS_HE[status] || status || "—";
}

function fillEngineSummary(health) {
  const engine = (health && health.engine) || {};
  const jobs = (health && health.jobs) || {};
  const streamJob = jobs.stream || {};
  const status = engineStatusHe(engine.status);
  const sweep = engine.sweep_seconds;
  let summary = status;
  if (sweep != null && Number.isFinite(Number(sweep))) {
    summary += ` · סריקה מלאה: ${Math.round(Number(sweep))} שניות`;
  }
  const watchCount = Number(engine.watch_count) || 0;
  const target = Number(engine.target_interval_seconds) || 0;
  if (watchCount > 0 && target > 0) {
    const goal = Math.round(watchCount * target);
    summary += ` · יעד: ~${goal} שניות`;
  }
  const streamErr = streamJob.last_error_message;
  const streamErrAt = streamJob.last_error_at;
  const streamOkAt = streamJob.last_success_at;
  if (
    streamErr &&
    (!streamOkAt || !streamErrAt || new Date(streamErrAt) >= new Date(streamOkAt))
  ) {
    summary += ` · ⚠ ${String(streamErr).slice(0, 80)}`;
  }
  byId("ov-engine-summary").textContent = summary;

  byId("ov-target-interval").textContent =
    target > 0 ? String(Math.round(target)) : "—";
}

function renderFreshnessTable(health) {
  const tbody = byId("ov-freshness-body");
  tbody.innerHTML = "";
  const items = state.watchItems || [];
  const asins = (health && health.asins) || {};
  const target = Number((health && health.engine && health.engine.target_interval_seconds) || 60);
  const staleThreshold = target * 3;

  if (!items.length) {
    const tr = document.createElement("tr");
    const td = document.createElement("td");
    td.colSpan = 3;
    td.className = "empty-cell";
    td.textContent = "אין מוצרים במעקב";
    tr.appendChild(td);
    tbody.appendChild(tr);
    return;
  }

  for (const item of items) {
    const asin = item.asin;
    const info = asins[asin] || {};
    const agoSec = secondsSince(info.last_checked);
    const stale = agoSec == null || agoSec > staleThreshold;

    const tr = document.createElement("tr");
    if (stale) {
      tr.classList.add("fresh-stale");
    }

    const asinTd = document.createElement("td");
    const code = document.createElement("code");
    code.textContent = asin;
    asinTd.appendChild(code);
    tr.appendChild(asinTd);

    const agoTd = document.createElement("td");
    agoTd.textContent = formatAgo(info.last_checked);
    tr.appendChild(agoTd);

    const resultTd = document.createElement("td");
    resultTd.textContent = formatResultHe(info);
    tr.appendChild(resultTd);

    tbody.appendChild(tr);
  }
}

function fillHealthOverview(health) {
  state.lastHealth = health;
  fillEngineSummary(health);
  renderFreshnessTable(health);
}

async function loadHealth() {
  try {
    const data = await apiJson("/api/health");
    fillHealthOverview(data.health || null);
  } catch (err) {
    if (!isAuthError(err)) {
      byId("ov-engine-summary").textContent = "לא ניתן לטעון מצב מנוע";
    }
  }
}

function startHealthPolling() {
  stopHealthPolling();
  state.healthPollId = window.setInterval(loadHealth, 15000);
}

function stopHealthPolling() {
  if (state.healthPollId) {
    window.clearInterval(state.healthPollId);
    state.healthPollId = null;
  }
}

/* ===== Bandwidth / overview ===== */

function formatMb(bytes) {
  const n = Number(bytes) || 0;
  return `${(n / (1024 * 1024)).toFixed(2)} MB`;
}

function formatGbFromBytes(bytes) {
  const n = Number(bytes) || 0;
  return `${(n / 1024 ** 3).toFixed(2)} GB`;
}

function fillBandwidthSummary(data) {
  const last = data.last_cycle;
  if (last && last.net_bytes_total != null) {
    byId("bw-last-cycle-mb").textContent = formatMb(last.net_bytes_total);
    byId("bw-last-cycle-time").textContent = last.recorded_at_il || "";
  } else {
    byId("bw-last-cycle-mb").textContent = "—";
    byId("bw-last-cycle-time").textContent = "אין נתונים עדיין";
  }

  const today = data.today;
  if (today && today.bytes_total != null) {
    byId("bw-today-gb").textContent = formatGbFromBytes(today.bytes_total);
    byId("ov-cycles-today").textContent = String(Number(today.cycles) || 0);
  } else {
    byId("bw-today-gb").textContent = "0.00 GB";
    byId("ov-cycles-today").textContent = "0";
  }

  const weekGb = Number(data.last_7_days_gb);
  byId("bw-week-gb").textContent = Number.isFinite(weekGb) ? `${weekGb.toFixed(2)} GB` : "—";

  const tbody = byId("bw-recent-body");
  tbody.innerHTML = "";
  const rows = data.recent_cycles || [];
  if (!rows.length) {
    const tr = document.createElement("tr");
    const td = document.createElement("td");
    td.colSpan = 2;
    td.className = "empty-cell";
    td.textContent = "אין נתונים עדיין";
    tr.appendChild(td);
    tbody.appendChild(tr);
    return;
  }
  for (const row of rows) {
    const tr = document.createElement("tr");
    const tsTd = document.createElement("td");
    tsTd.textContent = row.recorded_at_il || "?";
    const bytesTd = document.createElement("td");
    bytesTd.textContent = row.net_bytes_total != null ? formatMb(row.net_bytes_total) : "?";
    tr.appendChild(tsTd);
    tr.appendChild(bytesTd);
    tbody.appendChild(tr);
  }
}

async function loadBandwidthSummary() {
  try {
    const data = await apiJson("/api/bandwidth/summary");
    fillBandwidthSummary(data);
  } catch (err) {
    if (!isAuthError(err)) {
      byId("bw-last-cycle-mb").textContent = "שגיאה";
    }
  }
}

/* ===== sqlite-web ===== */

function renderSqliteStatus(status) {
  const line = byId("sqlite-status");
  const link = byId("sqlite-link");
  if (!status.running) {
    line.textContent = "מצב: לא פעיל";
    line.classList.remove("active");
    link.classList.add("hidden");
    link.href = "#";
    return;
  }
  const mode = status.read_only ? "קריאה בלבד" : "עריכה";
  line.textContent = `מצב: פעיל (${mode}) — נותרו ${status.seconds_remaining || 0} שניות`;
  line.classList.add("active");
  if (status.url) {
    link.href = status.url;
    link.classList.remove("hidden");
  } else {
    link.classList.add("hidden");
  }
}

async function refreshSqliteStatus(silent = true) {
  try {
    const status = await apiJson("/api/sqlite/status");
    renderSqliteStatus(status);
  } catch (err) {
    renderSqliteStatus({ running: false });
    if (!silent && !isAuthError(err)) {
      showToast("לא ניתן לבדוק את מצב הצפייה במסד", true);
    }
  }
}

async function startSqlite(readOnly) {
  try {
    const payload = await apiJson("/api/sqlite/start", {
      method: "POST",
      body: JSON.stringify({ read_only: !!readOnly }),
    });
    renderSqliteStatus(payload);
    showToast(readOnly ? "נפתחה צפייה במסד (קריאה בלבד)" : "נפתחה עריכת מסד (10 דקות)");
  } catch (err) {
    if (!isAuthError(err)) {
      showToast("לא ניתן לפתוח צפייה במסד", true);
    }
  }
}

async function stopSqlite() {
  try {
    await apiJson("/api/sqlite/stop", { method: "POST", body: JSON.stringify({}) });
    await refreshSqliteStatus();
    showToast("הצפייה במסד נסגרה");
  } catch (err) {
    if (!isAuthError(err)) {
      showToast("לא ניתן לסגור את הצפייה במסד", true);
    }
  }
}

/* ===== PM2 restart ===== */

async function restartPm2Stack() {
  const ok = window.confirm(
    "להפעיל מחדש את כל השירותים?\n\nהדף עלול להתנתק לכ־20 שניות ואז אפשר לרענן."
  );
  if (!ok) {
    return;
  }
  const button = byId("pm2-restart-btn");
  button.disabled = true;
  const prevText = button.textContent;
  button.textContent = "מפעיל מחדש...";
  try {
    const payload = await apiJson("/api/pm2/restart", {
      method: "POST",
      body: JSON.stringify({}),
    });
    showToast(payload.message || "המערכת מופעלת מחדש");
  } catch (err) {
    if (!isAuthError(err)) {
      showToast(err.message || "ההפעלה מחדש נכשלה", true);
    }
  } finally {
    button.disabled = false;
    button.textContent = prevText;
  }
}

/* ===== Alert feedback ===== */

const FEEDBACK_TYPE_HE = {
  back_in_stock: "חזר למלאי",
  new_product: "מוצר חדש",
  price_drop: "ירידת מחיר",
  price_below_target: "מתחת למחיר היעד",
};

const FEEDBACK_TAGS = [
  { id: "late", label: "הגיע באיחור" },
  { id: "wrong_price", label: "מחיר שגוי" },
  { id: "duplicate", label: "התראה כפולה" },
  { id: "irrelevant", label: "לא רלוונטי" },
];

function ilParts(iso) {
  if (!iso) {
    return null;
  }
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) {
    return null;
  }
  const fmt = new Intl.DateTimeFormat("en-GB", {
    timeZone: "Asia/Jerusalem",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
  const parts = {};
  for (const p of fmt.formatToParts(d)) {
    parts[p.type] = p.value;
  }
  return parts;
}

function formatIlShort(iso) {
  const p = ilParts(iso);
  if (!p) {
    return "—";
  }
  return `${p.day}/${p.month} ${p.hour}:${p.minute}`;
}

function truncateTitle(title, asin) {
  const t = String(title || "").trim();
  if (!t) {
    return asin;
  }
  return t.length > 60 ? `${t.slice(0, 59)}…` : t;
}

function formatPriceHe(price) {
  if (price == null || !Number.isFinite(Number(price))) {
    return "ללא מחיר";
  }
  return `$${Number(price).toFixed(2)}`;
}

function feedbackSnapshot(row) {
  return JSON.stringify({
    verdict: row.verdict || null,
    tags: [...row.tags].sort(),
    note: String(row.note || "").trim(),
    seenDate: row.seenDate || "",
    seenTime: row.seenTime || "",
  });
}

function buildFeedbackRow(alert) {
  const alertParts = ilParts(alert.sent_at);
  const existing = alert.feedback || null;

  const rowState = {
    alertId: alert.alert_id,
    verdict: existing ? existing.verdict || null : null,
    tags: existing && Array.isArray(existing.tags) ? [...existing.tags] : [],
    note: existing ? existing.note || "" : "",
    seenDate: "",
    seenTime: "",
  };

  // seen_at defaults to the alert's IL date; existing feedback overrides.
  if (existing && existing.seen_at && existing.seen_at.includes("T")) {
    const [d, t] = existing.seen_at.split("T");
    rowState.seenDate = d;
    rowState.seenTime = t;
  } else if (alertParts) {
    rowState.seenDate = `${alertParts.year}-${alertParts.month}-${alertParts.day}`;
  }

  const card = document.createElement("div");
  card.className = "feedback-row";
  if (rowState.verdict === "like") {
    card.classList.add("fb-liked");
  } else if (rowState.verdict === "dislike") {
    card.classList.add("fb-disliked");
  }

  // Header line: time · type · title · price
  const head = document.createElement("div");
  head.className = "feedback-row-head";
  const time = document.createElement("span");
  time.className = "fb-time";
  time.textContent = formatIlShort(alert.sent_at);
  const type = document.createElement("span");
  type.className = "fb-type";
  type.textContent = FEEDBACK_TYPE_HE[alert.alert_type] || alert.alert_type;
  const title = document.createElement("span");
  title.className = "fb-title";
  title.textContent = truncateTitle(alert.title, alert.asin);
  title.title = alert.title || alert.asin;
  const price = document.createElement("span");
  price.className = "fb-price";
  price.textContent = formatPriceHe(alert.price);
  head.append(time, type, title, price);
  card.appendChild(head);

  // Controls
  const controls = document.createElement("div");
  controls.className = "feedback-controls";

  const saveBtn = document.createElement("button");
  saveBtn.type = "button";
  saveBtn.className = "btn btn-primary fb-save";
  saveBtn.textContent = "שמירה";

  const baseline = { value: feedbackSnapshot(rowState) };
  const refreshSave = () => {
    saveBtn.classList.remove("fb-save-ok", "fb-save-err");
    saveBtn.textContent = "שמירה";
    saveBtn.disabled = feedbackSnapshot(rowState) === baseline.value;
  };

  // Verdict toggle
  const verdictWrap = document.createElement("div");
  verdictWrap.className = "fb-verdict";
  const likeBtn = document.createElement("button");
  likeBtn.type = "button";
  likeBtn.className = "fb-verdict-btn fb-like";
  likeBtn.textContent = "👍";
  const dislikeBtn = document.createElement("button");
  dislikeBtn.type = "button";
  dislikeBtn.className = "fb-verdict-btn fb-dislike";
  dislikeBtn.textContent = "👎";
  const syncVerdict = () => {
    likeBtn.classList.toggle("selected", rowState.verdict === "like");
    dislikeBtn.classList.toggle("selected", rowState.verdict === "dislike");
    card.classList.toggle("fb-liked", rowState.verdict === "like");
    card.classList.toggle("fb-disliked", rowState.verdict === "dislike");
  };
  likeBtn.addEventListener("click", () => {
    rowState.verdict = rowState.verdict === "like" ? null : "like";
    syncVerdict();
    refreshSave();
  });
  dislikeBtn.addEventListener("click", () => {
    rowState.verdict = rowState.verdict === "dislike" ? null : "dislike";
    syncVerdict();
    refreshSave();
  });
  verdictWrap.append(likeBtn, dislikeBtn);
  syncVerdict();

  // Tag chips
  const tagsWrap = document.createElement("div");
  tagsWrap.className = "fb-tags";
  for (const tag of FEEDBACK_TAGS) {
    const chip = document.createElement("button");
    chip.type = "button";
    chip.className = "fb-chip";
    chip.textContent = tag.label;
    chip.classList.toggle("selected", rowState.tags.includes(tag.id));
    chip.addEventListener("click", () => {
      const idx = rowState.tags.indexOf(tag.id);
      if (idx >= 0) {
        rowState.tags.splice(idx, 1);
      } else {
        rowState.tags.push(tag.id);
      }
      chip.classList.toggle("selected", rowState.tags.includes(tag.id));
      refreshSave();
    });
    tagsWrap.appendChild(chip);
  }

  // Note
  const note = document.createElement("input");
  note.type = "text";
  note.className = "fb-note";
  note.maxLength = 500;
  note.placeholder = "הערה...";
  note.value = rowState.note;
  note.addEventListener("input", () => {
    rowState.note = note.value;
    refreshSave();
  });

  // Seen date + time
  const seenWrap = document.createElement("div");
  seenWrap.className = "fb-seen";
  const seenLabel = document.createElement("span");
  seenLabel.className = "fb-seen-label";
  seenLabel.textContent = "מתי ראית את השינוי באמזון?";
  const seenDate = document.createElement("input");
  seenDate.type = "date";
  seenDate.className = "fb-seen-date";
  seenDate.value = rowState.seenDate;
  const seenTime = document.createElement("input");
  seenTime.type = "time";
  seenTime.className = "fb-seen-time";
  seenTime.value = rowState.seenTime;
  seenDate.addEventListener("input", () => {
    rowState.seenDate = seenDate.value;
    refreshSave();
  });
  seenTime.addEventListener("input", () => {
    rowState.seenTime = seenTime.value;
    refreshSave();
  });
  seenWrap.append(seenLabel, seenDate, seenTime);

  // Save handler
  saveBtn.addEventListener("click", async () => {
    let seenAt = null;
    if (rowState.seenDate && rowState.seenTime) {
      seenAt = `${rowState.seenDate}T${rowState.seenTime}`;
    }
    const body = {
      alert_id: rowState.alertId,
      verdict: rowState.verdict,
      tags: rowState.tags,
      note: String(rowState.note || "").trim(),
      seen_at: seenAt,
    };
    saveBtn.disabled = true;
    try {
      await apiJson("/api/alerts/feedback", {
        method: "POST",
        body: JSON.stringify(body),
      });
      baseline.value = feedbackSnapshot(rowState);
      saveBtn.textContent = "✓ נשמר";
      saveBtn.classList.add("fb-save-ok");
      saveBtn.disabled = true;
    } catch (err) {
      saveBtn.disabled = false;
      saveBtn.textContent = "שמירה נכשלה";
      saveBtn.classList.add("fb-save-err");
      if (!isAuthError(err)) {
        showToast(`שגיאה בשמירת משוב: ${err.message}`, true);
      }
    }
  });

  controls.append(verdictWrap, tagsWrap, note, seenWrap, saveBtn);
  card.appendChild(controls);
  refreshSave();
  return card;
}

async function loadFeedback() {
  const list = byId("feedback-list");
  const days = String(byId("feedback-days").value || "7");
  list.innerHTML = '<p class="empty-cell">טוען...</p>';
  try {
    const payload = await apiJson(`/api/alerts/recent?days=${encodeURIComponent(days)}`);
    const alerts = payload.alerts || [];
    list.innerHTML = "";
    if (!alerts.length) {
      const empty = document.createElement("p");
      empty.className = "empty-cell";
      empty.textContent = "אין התראות בטווח הזמן שנבחר";
      list.appendChild(empty);
      return;
    }
    for (const alert of alerts) {
      list.appendChild(buildFeedbackRow(alert));
    }
  } catch (err) {
    if (!isAuthError(err)) {
      list.innerHTML = "";
      const error = document.createElement("p");
      error.className = "empty-cell";
      error.textContent = "לא ניתן לטעון התראות";
      list.appendChild(error);
    }
  }
}

function openFeedbackModal() {
  byId("feedback-modal").classList.remove("hidden");
  loadFeedback();
}

function closeFeedbackModal() {
  byId("feedback-modal").classList.add("hidden");
}

/* ===== Wiring ===== */

function bindEvents() {
  byId("login-form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const user = String(byId("login-user").value || "").trim();
    const pass = String(byId("login-pass").value || "");
    setAuth(user, pass);
    try {
      await loadAppData();
      showApp();
      startSqlitePolling();
      startHealthPolling();
    } catch (err) {
      clearAuth();
      if (err.message === "unauthorized") {
        return;
      }
      showLogin(err.message === "not_logged_in" ? "יש להתחבר" : `שגיאה: ${err.message}`);
    }
  });

  byId("logout-btn").addEventListener("click", () => {
    clearAuth();
    stopSqlitePolling();
    stopHealthPolling();
    showLogin();
  });

  document.querySelectorAll(".nav-item[data-tab]").forEach((btn) => {
    btn.addEventListener("click", () => switchTab(btn.dataset.tab));
  });

  byId("watch-add-btn").addEventListener("click", () => addAsin("watch"));
  byId("blacklist-add-btn").addEventListener("click", () => addAsin("blacklist"));
  byId("watch-asin-input").addEventListener("keydown", (ev) => {
    if (ev.key === "Enter") { ev.preventDefault(); addAsin("watch"); }
  });
  byId("blacklist-asin-input").addEventListener("keydown", (ev) => {
    if (ev.key === "Enter") { ev.preventDefault(); addAsin("blacklist"); }
  });
  byId("watch-bulk-btn").addEventListener("click", () => openBulkModal("watch"));
  byId("blacklist-bulk-btn").addEventListener("click", () => openBulkModal("blacklist"));
  byId("bulk-cancel-btn").addEventListener("click", closeBulkModal);
  byId("bulk-apply-btn").addEventListener("click", applyBulk);
  byId("bulk-modal").addEventListener("click", (ev) => {
    if (ev.target === byId("bulk-modal")) {
      closeBulkModal();
    }
  });
  document.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape" && !byId("bulk-modal").classList.contains("hidden")) {
      closeBulkModal();
    }
  });

  byId("feedback-open-btn").addEventListener("click", openFeedbackModal);
  byId("feedback-close-btn").addEventListener("click", closeFeedbackModal);
  byId("feedback-days").addEventListener("change", loadFeedback);
  byId("feedback-modal").addEventListener("click", (ev) => {
    if (ev.target === byId("feedback-modal")) {
      closeFeedbackModal();
    }
  });
  document.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape" && !byId("feedback-modal").classList.contains("hidden")) {
      closeFeedbackModal();
    }
  });

  byId("save-btn").addEventListener("click", saveSettings);
  byId("discard-btn").addEventListener("click", discardChanges);
  byId("pm2-restart-btn").addEventListener("click", restartPm2Stack);
  byId("sqlite-ro-btn").addEventListener("click", () => startSqlite(true));
  byId("sqlite-rw-btn").addEventListener("click", () => startSqlite(false));
  byId("sqlite-stop-btn").addEventListener("click", stopSqlite);

  bindDirtyTracking();

  window.addEventListener("beforeunload", (ev) => {
    if (isDirty()) {
      ev.preventDefault();
      ev.returnValue = "";
    }
  });
}

function startSqlitePolling() {
  stopSqlitePolling();
  state.sqlitePollId = window.setInterval(refreshSqliteStatus, 2000);
}

function stopSqlitePolling() {
  if (state.sqlitePollId) {
    window.clearInterval(state.sqlitePollId);
    state.sqlitePollId = null;
  }
}

async function loadAppData() {
  await loadSettings();
  await Promise.all([
    loadBandwidthSummary(),
    loadHealth(),
    loadAsins("watch"),
    loadAsins("blacklist"),
    refreshSqliteStatus(),
  ]);
}

async function init() {
  bindEvents();
  restoreTab();
  if (getAuthHeader()) {
    try {
      await loadAppData();
      showApp();
      startSqlitePolling();
      startHealthPolling();
      return;
    } catch (err) {
      clearAuth();
      if (err.message === "unauthorized") {
        return;
      }
    }
  }
  showLogin();
}

window.addEventListener("beforeunload", () => {
  stopSqlitePolling();
  stopHealthPolling();
});
window.addEventListener("DOMContentLoaded", init);
