const AUTH_KEY = "admin_ui_auth";

const state = {
  sqlitePollId: null,
  bulkRole: null,
  toastTimer: null,
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

function getAuthHeader() {
  const token = sessionStorage.getItem(AUTH_KEY);
  if (!token) {
    return null;
  }
  return `Basic ${token}`;
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

async function apiJson(url, options = {}) {
  const auth = getAuthHeader();
  if (!auth) {
    showLogin("יש להתחברות קודם");
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
    const msg = userFacingError(payload, res.status);
    throw new Error(msg);
  }
  return payload;
}

function renderAsinTable(role, items) {
  const tbody = byId(role === "watch" ? "watch-body" : "blacklist-body");
  tbody.innerHTML = "";
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

    const actionTd = document.createElement("td");
    const removeBtn = document.createElement("button");
    removeBtn.type = "button";
    removeBtn.className = "btn btn-danger";
    removeBtn.textContent = "הסר";
    removeBtn.addEventListener("click", async () => {
      try {
        await apiJson(`/api/asins/${encodeURIComponent(item.asin)}/${role}`, { method: "DELETE" });
        showToast(`קוד ${item.asin} הוסר`);
        await loadAsins(role);
      } catch (err) {
        if (err.message !== "unauthorized" && err.message !== "not_logged_in") {
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
  byId("pdp-poll-minutes").value = settings.pdp_poll_minutes ?? "";
  byId("max-cycle-seconds").value = settings.max_cycle_seconds ?? "";
  byId("max-concurrent-tabs").value = settings.pdp_watch_max_concurrent_tabs ?? "";
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
  byId("tpl-default").value = templates.default || "";
}

async function loadSettings() {
  const payload = await apiJson("/api/settings");
  fillSettings(payload.settings || {});
}

async function addAsin(role) {
  const asinInput = byId(role === "watch" ? "watch-asin-input" : "blacklist-asin-input");
  const notesInput = byId(role === "watch" ? "watch-notes-input" : "blacklist-notes-input");
  const asin = String(asinInput.value || "").trim().toUpperCase();
  const notes = String(notesInput.value || "").trim();
  if (!isValidAsin(asin)) {
    showToast("יש להזין קוד מוצר תקין — 10 תווים באנגלית", true);
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
    showToast(`קוד ${asin} נוסף`);
    await loadAsins(role);
  } catch (err) {
    if (err.message !== "unauthorized" && err.message !== "not_logged_in") {
      showToast(`שגיאה בהוספה: ${err.message}`, true);
    }
  }
}

function openBulkModal(role) {
  state.bulkRole = role;
  byId("bulk-modal-title").textContent =
    role === "watch" ? "הדבקה מרובה — מעקב מוצרים" : "הדבקה מרובה — רשימה שחורה";
  byId("bulk-text").value = "";
  byId("bulk-modal").classList.remove("hidden");
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
  const raw = byId("bulk-text").value;
  const parts = raw
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
    showToast("לא נמצאו קודי מוצר תקינים להוספה", true);
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
      // continue
    }
  }
  closeBulkModal();
  await loadAsins(role);
  showToast(`נוספו ${success} מתוך ${valid.length} מוצרים`);
}

function collectSettingsPayload() {
  const tabs = parsePositiveInt(byId("max-concurrent-tabs").value, 2);
  return {
    pdp_poll_minutes: parsePositiveInt(byId("pdp-poll-minutes").value, 4),
    max_cycle_seconds: parsePositiveInt(byId("max-cycle-seconds").value, 170),
    pdp_watch_max_concurrent_tabs: Math.min(10, tabs),
    playwright_headless: byId("playwright-headless").checked,
    wa_group_id: String(byId("wa-group-id").value || "").trim(),
    wa_client_to: String(byId("wa-client-to").value || "").trim(),
    price_drop_percent: parsePercent(byId("price-drop-percent").value, 10),
    max_requests_per_minute: parsePositiveInt(byId("max-requests-per-minute").value, 10),
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
      default: byId("tpl-default").value,
    },
  };
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
    showToast("ההגדרות נשמרו בהצלחה");
  } catch (err) {
    if (err.message !== "unauthorized" && err.message !== "not_logged_in") {
      showToast(`שמירה נכשלה: ${err.message}`, true);
    }
  } finally {
    button.disabled = false;
    button.textContent = "שמור";
  }
}

function renderSqliteStatus(status) {
  const line = byId("sqlite-status");
  const link = byId("sqlite-link");
  if (!status.running) {
    line.textContent = "מצב: לא פעיל";
    link.classList.add("hidden");
    link.href = "#";
    return;
  }
  const mode = status.read_only ? "קריאה בלבד" : "עריכה";
  line.textContent = `מצב: פעיל (${mode}) - נותרו ${status.seconds_remaining || 0} שניות`;
  if (status.url) {
    link.href = status.url;
    link.classList.remove("hidden");
  } else {
    link.classList.add("hidden");
  }
}

async function refreshSqliteStatus() {
  try {
    const status = await apiJson("/api/sqlite/status");
    renderSqliteStatus(status);
  } catch (err) {
    renderSqliteStatus({ running: false });
    if (err.message !== "unauthorized" && err.message !== "not_logged_in") {
      showToast("לא ניתן לבדוק מצב צפייה במסד", true);
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
    if (err.message !== "unauthorized" && err.message !== "not_logged_in") {
      showToast("לא ניתן לפתוח צפייה במסד", true);
    }
  }
}

async function restartPm2Stack() {
  const ok = window.confirm(
    "להפעיל מחדש את כל השירותים?\n\nהדף עלול להתנתק. אחרי כ-20 שניות פתחו שוב את הפאנל."
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
    showToast(payload.message || "המערכת הופעלה מחדש");
  } catch (err) {
    if (err.message !== "unauthorized" && err.message !== "not_logged_in") {
      showToast(err.message || "הפעלה מחדש נכשלה", true);
    }
  } finally {
    button.disabled = false;
    button.textContent = prevText;
  }
}

async function stopSqlite() {
  try {
    await apiJson("/api/sqlite/stop", { method: "POST", body: JSON.stringify({}) });
    await refreshSqliteStatus();
    showToast("צפייה במסד נסגרה");
  } catch (err) {
    if (err.message !== "unauthorized" && err.message !== "not_logged_in") {
      showToast("לא ניתן לסגור צפייה במסד", true);
    }
  }
}

function bindEvents() {
  byId("login-form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const user = String(byId("login-user").value || "").trim();
    const pass = String(byId("login-pass").value || "");
    setAuth(user, pass);
    try {
      await loadAppData();
      showApp();
    } catch (err) {
      clearAuth();
      if (err.message === "unauthorized") {
        return;
      }
      showLogin(err.message === "not_logged_in" ? "יש להתחברות" : `שגיאה: ${err.message}`);
    }
  });

  byId("watch-add-btn").addEventListener("click", () => addAsin("watch"));
  byId("blacklist-add-btn").addEventListener("click", () => addAsin("blacklist"));
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
  byId("save-btn").addEventListener("click", saveSettings);
  byId("pm2-restart-btn").addEventListener("click", restartPm2Stack);
  byId("sqlite-ro-btn").addEventListener("click", () => startSqlite(true));
  byId("sqlite-rw-btn").addEventListener("click", () => startSqlite(false));
  byId("sqlite-stop-btn").addEventListener("click", stopSqlite);
}

async function loadAppData() {
  await loadSettings();
  await Promise.all([loadAsins("watch"), loadAsins("blacklist"), refreshSqliteStatus()]);
}

async function init() {
  bindEvents();
  if (getAuthHeader()) {
    try {
      await loadAppData();
      showApp();
      state.sqlitePollId = window.setInterval(refreshSqliteStatus, 1000);
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
  if (state.sqlitePollId) {
    window.clearInterval(state.sqlitePollId);
  }
});

window.addEventListener("DOMContentLoaded", init);
