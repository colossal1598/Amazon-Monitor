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

function showToast(message, isError = false) {
  const toast = byId("toast");
  toast.textContent = message;
  toast.classList.toggle("error", !!isError);
  toast.classList.remove("hidden");
  if (state.toastTimer) {
    window.clearTimeout(state.toastTimer);
  }
  state.toastTimer = window.setTimeout(() => toast.classList.add("hidden"), 2800);
}

async function apiJson(url, options = {}) {
  const opts = { ...options };
  const headers = { ...(opts.headers || {}) };
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
  if (!res.ok) {
    const msg = payload.message || payload.error || `HTTP ${res.status}`;
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
        showToast(`ASIN ${item.asin} הוסר`);
        await loadAsins(role);
      } catch (err) {
        showToast(`שגיאה בהסרה: ${err.message}`, true);
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

function fillSettings(settings) {
  byId("aes-url").value = (((settings.search_urls || {}).aes_llc) || "");
  byId("pdp-poll-minutes").value = settings.pdp_poll_minutes ?? "";
  byId("max-cycle-seconds").value = settings.max_cycle_seconds ?? "";
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
    showToast("יש להזין ASIN תקין באורך 10 תווים", true);
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
    showToast(`ASIN ${asin} נוסף`);
    await loadAsins(role);
  } catch (err) {
    showToast(`שגיאה בהוספה: ${err.message}`, true);
  }
}

function openBulkModal(role) {
  state.bulkRole = role;
  byId("bulk-modal-title").textContent = role === "watch" ? "הדבקה מרובה - מעקב PDP" : "הדבקה מרובה - רשימה שחורה";
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
  const parts = raw.split(/[^A-Za-z0-9]+/g).map((x) => x.trim().toUpperCase()).filter(Boolean);
  const seen = new Set();
  const valid = [];
  for (const value of parts) {
    if (!seen.has(value) && isValidAsin(value)) {
      seen.add(value);
      valid.push(value);
    }
  }
  if (!valid.length) {
    showToast("לא נמצאו ASIN תקינים להוספה", true);
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
      // Continue adding remaining values, then refresh once.
    }
  }
  closeBulkModal();
  await loadAsins(role);
  showToast(`נוספו ${success} מתוך ${valid.length} ASIN`);
}

function collectSettingsPayload() {
  const pollMinutes = parseInt(byId("pdp-poll-minutes").value || "0", 10);
  const maxCycleSeconds = parseInt(byId("max-cycle-seconds").value || "0", 10);
  return {
    search_urls: {
      aes_llc: String(byId("aes-url").value || "").trim(),
    },
    pdp_poll_minutes: Number.isFinite(pollMinutes) && pollMinutes > 0 ? pollMinutes : 4,
    max_cycle_seconds: Number.isFinite(maxCycleSeconds) && maxCycleSeconds > 0 ? maxCycleSeconds : 170,
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
    showToast(`שמירה נכשלה: ${err.message}`, true);
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
    showToast(`שגיאת סטטוס sqlite-web: ${err.message}`, true);
  }
}

async function startSqlite(readOnly) {
  try {
    const payload = await apiJson("/api/sqlite/start", {
      method: "POST",
      body: JSON.stringify({ read_only: !!readOnly }),
    });
    renderSqliteStatus(payload);
    showToast(readOnly ? "sqlite-web הופעל בקריאה בלבד" : "sqlite-web הופעל במצב עריכה");
  } catch (err) {
    showToast(`הפעלת sqlite-web נכשלה: ${err.message}`, true);
  }
}

async function stopSqlite() {
  try {
    await apiJson("/api/sqlite/stop", { method: "POST", body: JSON.stringify({}) });
    await refreshSqliteStatus();
    showToast("sqlite-web נסגר");
  } catch (err) {
    showToast(`סגירת sqlite-web נכשלה: ${err.message}`, true);
  }
}

function bindEvents() {
  byId("watch-add-btn").addEventListener("click", () => addAsin("watch"));
  byId("blacklist-add-btn").addEventListener("click", () => addAsin("blacklist"));
  byId("watch-bulk-btn").addEventListener("click", () => openBulkModal("watch"));
  byId("blacklist-bulk-btn").addEventListener("click", () => openBulkModal("blacklist"));
  byId("bulk-cancel-btn").addEventListener("click", closeBulkModal);
  byId("bulk-apply-btn").addEventListener("click", applyBulk);
  byId("save-btn").addEventListener("click", saveSettings);
  byId("sqlite-ro-btn").addEventListener("click", () => startSqlite(true));
  byId("sqlite-rw-btn").addEventListener("click", () => startSqlite(false));
  byId("sqlite-stop-btn").addEventListener("click", stopSqlite);
}

async function init() {
  bindEvents();
  try {
    await loadSettings();
    await Promise.all([loadAsins("watch"), loadAsins("blacklist"), refreshSqliteStatus()]);
  } catch (err) {
    showToast(`טעינת נתונים נכשלה: ${err.message}`, true);
  }
  state.sqlitePollId = window.setInterval(refreshSqliteStatus, 1000);
}

window.addEventListener("beforeunload", () => {
  if (state.sqlitePollId) {
    window.clearInterval(state.sqlitePollId);
  }
});

window.addEventListener("DOMContentLoaded", init);
