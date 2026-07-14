/* Alert-feedback page. Self-contained: shares the admin session with the main
   panel via the same sessionStorage key, but carries its own login gate so a
   direct visit (client opening /feedback.html cold) still authenticates. */

const AUTH_KEY = "admin_ui_auth";
const RANGE_KEY = "admin_ui_feedback_days";

const state = {
  toastTimer: null,
};

function byId(id) {
  return document.getElementById(id);
}

/* ===== Auth (mirrors app.js so the session carries across pages) ===== */

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

function isAuthError(err) {
  return err && (err.message === "unauthorized" || err.message === "not_logged_in");
}

function showLogin(errMsg) {
  byId("feedback-app").classList.add("hidden");
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
  byId("feedback-app").classList.remove("hidden");
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
    const code = payload && (payload.message || payload.error);
    throw new Error(code || "request_failed");
  }
  return payload;
}

/* ===== Feedback domain ===== */

const TYPE_HE = {
  back_in_stock: "חזר למלאי",
  new_product: "מוצר חדש",
  price_drop: "ירידת מחיר",
  price_below_target: "מתחת למחיר היעד",
};

const TAGS = [
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
  return `${p.day}/${p.month} · ${p.hour}:${p.minute}`;
}

function formatPriceHe(price) {
  if (price == null || !Number.isFinite(Number(price))) {
    return "ללא מחיר";
  }
  return `$${Number(price).toFixed(2)}`;
}

function buildCard(alert) {
  const alertParts = ilParts(alert.sent_at);
  const existing = alert.feedback || null;

  const st = {
    alertId: alert.alert_id,
    verdict: existing ? existing.verdict || null : null,
    tags: existing && Array.isArray(existing.tags) ? [...existing.tags] : [],
    note: existing ? existing.note || "" : "",
    seenDate: "",
    seenTime: "",
    saving: false,
    savedOnce: !!existing,
  };
  if (existing && existing.seen_at && existing.seen_at.includes("T")) {
    const [d, t] = existing.seen_at.split("T");
    st.seenDate = d;
    st.seenTime = t;
  } else if (alertParts) {
    st.seenDate = `${alertParts.year}-${alertParts.month}-${alertParts.day}`;
  }

  const card = document.createElement("article");
  card.className = "fbc";

  // --- Header: pill + time, then dominant title, then secondary price ---
  const head = document.createElement("div");
  head.className = "fbc-head";

  const topRow = document.createElement("div");
  topRow.className = "fbc-toprow";
  const pill = document.createElement("span");
  pill.className = `fbc-pill fbc-pill-${alert.alert_type}`;
  pill.textContent = TYPE_HE[alert.alert_type] || alert.alert_type;
  const time = document.createElement("time");
  time.className = "fbc-time";
  time.textContent = formatIlShort(alert.sent_at);
  topRow.append(pill, time);

  const title = document.createElement("h2");
  title.className = "fbc-title";
  title.textContent = String(alert.title || "").trim() || alert.asin;
  title.title = alert.title || alert.asin;

  const price = document.createElement("div");
  price.className = "fbc-price";
  price.textContent = formatPriceHe(alert.price);

  head.append(topRow, title, price);
  card.appendChild(head);

  // --- Saved status line (per-card, unmistakable) ---
  const status = document.createElement("div");
  status.className = "fbc-status";
  const syncStatus = (text, kind) => {
    status.textContent = text || "";
    status.classList.toggle("ok", kind === "ok");
    status.classList.toggle("err", kind === "err");
    status.classList.toggle("saving", kind === "saving");
  };

  // --- Rating: large thumb-friendly buttons (primary action) ---
  const rate = document.createElement("div");
  rate.className = "fbc-rate";
  const upBtn = document.createElement("button");
  upBtn.type = "button";
  upBtn.className = "fbc-thumb fbc-up";
  upBtn.innerHTML = '<span class="fbc-thumb-emoji">👍</span><span class="fbc-thumb-label">התראה טובה</span>';
  const downBtn = document.createElement("button");
  downBtn.type = "button";
  downBtn.className = "fbc-thumb fbc-down";
  downBtn.innerHTML = '<span class="fbc-thumb-emoji">👎</span><span class="fbc-thumb-label">התראה בעייתית</span>';
  rate.append(upBtn, downBtn);

  // --- Details drawer (tags / note / seen), progressively revealed ---
  const details = document.createElement("div");
  details.className = "fbc-details";

  const tagsWrap = document.createElement("div");
  tagsWrap.className = "fbc-tags";
  const chipEls = new Map();
  for (const tag of TAGS) {
    const chip = document.createElement("button");
    chip.type = "button";
    chip.className = "fbc-chip";
    chip.textContent = tag.label;
    chip.classList.toggle("selected", st.tags.includes(tag.id));
    chip.addEventListener("click", () => {
      const idx = st.tags.indexOf(tag.id);
      if (idx >= 0) {
        st.tags.splice(idx, 1);
      } else {
        st.tags.push(tag.id);
      }
      chip.classList.toggle("selected", st.tags.includes(tag.id));
      save();
    });
    chipEls.set(tag.id, chip);
    tagsWrap.appendChild(chip);
  }

  const note = document.createElement("input");
  note.type = "text";
  note.className = "fbc-note";
  note.maxLength = 500;
  note.placeholder = "הערה (לא חובה)...";
  note.value = st.note;
  note.addEventListener("blur", () => {
    if (note.value !== st.note) {
      st.note = note.value;
      save();
    }
  });
  note.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter") {
      ev.preventDefault();
      note.blur();
    }
  });

  const seen = document.createElement("div");
  seen.className = "fbc-seen";
  const seenLabel = document.createElement("span");
  seenLabel.className = "fbc-seen-label";
  seenLabel.textContent = "מתי ראית את השינוי באמזון?";
  const seenInputs = document.createElement("div");
  seenInputs.className = "fbc-seen-inputs";
  const seenDate = document.createElement("input");
  seenDate.type = "date";
  seenDate.className = "fbc-seen-date";
  seenDate.value = st.seenDate;
  const seenTime = document.createElement("input");
  seenTime.type = "time";
  seenTime.className = "fbc-seen-time";
  seenTime.value = st.seenTime;
  seenDate.addEventListener("change", () => {
    st.seenDate = seenDate.value;
    save();
  });
  seenTime.addEventListener("change", () => {
    st.seenTime = seenTime.value;
    save();
  });
  seenInputs.append(seenDate, seenTime);
  seen.append(seenLabel, seenInputs);

  details.append(tagsWrap, note, seen);

  // --- Expand toggle ---
  const more = document.createElement("button");
  more.type = "button";
  more.className = "fbc-more";
  const setExpanded = (open) => {
    details.classList.toggle("open", open);
    more.classList.toggle("open", open);
    more.textContent = open ? "פחות פרטים ▲" : "פרטים נוספים ▾";
  };
  more.addEventListener("click", () => setExpanded(!details.classList.contains("open")));

  // --- Verdict rendering + save ---
  const syncVerdict = () => {
    upBtn.classList.toggle("selected", st.verdict === "like");
    downBtn.classList.toggle("selected", st.verdict === "dislike");
    card.classList.toggle("fbc-liked", st.verdict === "like");
    card.classList.toggle("fbc-disliked", st.verdict === "dislike");
  };

  async function save() {
    if (st.saving) {
      // Coalesce: mark that another save is needed once the in-flight one lands.
      st.pending = true;
      return;
    }
    st.saving = true;
    st.pending = false;
    syncStatus("שומר...", "saving");
    let seenAt = null;
    if (st.seenDate && st.seenTime) {
      seenAt = `${st.seenDate}T${st.seenTime}`;
    }
    const body = {
      alert_id: st.alertId,
      verdict: st.verdict,
      tags: st.tags,
      note: String(st.note || "").trim(),
      seen_at: seenAt,
    };
    try {
      await apiJson("/api/alerts/feedback", {
        method: "POST",
        body: JSON.stringify(body),
      });
      st.savedOnce = true;
      card.classList.add("fbc-saved");
      syncStatus("✓ נשמר", "ok");
    } catch (err) {
      if (!isAuthError(err)) {
        syncStatus("✗ השמירה נכשלה — נסו שוב", "err");
        showToast("שגיאה בשמירת המשוב", true);
      }
    } finally {
      st.saving = false;
      if (st.pending) {
        save();
      }
    }
  }

  upBtn.addEventListener("click", () => {
    st.verdict = st.verdict === "like" ? null : "like";
    syncVerdict();
    if (st.verdict) {
      setExpanded(true);
    }
    save();
  });
  downBtn.addEventListener("click", () => {
    st.verdict = st.verdict === "dislike" ? null : "dislike";
    syncVerdict();
    if (st.verdict) {
      setExpanded(true);
    }
    save();
  });

  card.append(rate, status, more, details);
  syncVerdict();
  if (st.savedOnce) {
    card.classList.add("fbc-saved");
    syncStatus("✓ נשמר", "ok");
    setExpanded(true);
  } else {
    setExpanded(false);
  }
  return card;
}

async function loadFeedback() {
  const list = byId("feedback-list");
  const days = String(byId("feedback-days").value || "7");
  sessionStorage.setItem(RANGE_KEY, days);
  list.innerHTML = '<div class="fb-state fb-loading">טוען התראות...</div>';
  try {
    const payload = await apiJson(`/api/alerts/recent?days=${encodeURIComponent(days)}`);
    const alerts = payload.alerts || [];
    list.innerHTML = "";
    if (!alerts.length) {
      const empty = document.createElement("div");
      empty.className = "fb-state fb-empty";
      empty.innerHTML = '<div class="fb-empty-icon">📭</div><p>אין התראות בטווח שנבחר</p>';
      list.appendChild(empty);
      return;
    }
    for (const alert of alerts) {
      list.appendChild(buildCard(alert));
    }
  } catch (err) {
    if (!isAuthError(err)) {
      list.innerHTML = '<div class="fb-state fb-empty"><p>לא ניתן לטעון התראות</p></div>';
    }
  }
}

/* ===== Wiring ===== */

function bindEvents() {
  byId("login-form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const user = String(byId("login-user").value || "").trim();
    const pass = String(byId("login-pass").value || "");
    setAuth(user, pass);
    try {
      await loadFeedback();
      showApp();
    } catch (err) {
      clearAuth();
      if (err.message === "unauthorized") {
        return;
      }
      showLogin(err.message === "not_logged_in" ? "יש להתחבר" : "שגיאה בהתחברות");
    }
  });

  byId("logout-btn").addEventListener("click", () => {
    clearAuth();
    showLogin();
  });

  byId("feedback-days").addEventListener("change", loadFeedback);
  byId("feedback-refresh").addEventListener("click", loadFeedback);
}

async function init() {
  bindEvents();
  const savedRange = sessionStorage.getItem(RANGE_KEY);
  if (savedRange && byId("feedback-days").querySelector(`option[value="${savedRange}"]`)) {
    byId("feedback-days").value = savedRange;
  }
  if (getAuthHeader()) {
    try {
      await loadFeedback();
      showApp();
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

window.addEventListener("DOMContentLoaded", init);
