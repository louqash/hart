// Small helpers shared by all pages: JSON API calls (with the CSRF header the
// server requires on writes), toasts, and form serialisation.

const api = {
  async request(method, url, body) {
    const opts = { method, headers: { "X-Requested-With": "hart" } };
    if (body !== undefined) {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
    const res = await fetch(url, opts);
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      const detail = data.error?.message ?? data.detail;
      const message = Array.isArray(detail)
        ? detail.map((d) => `${(d.loc || []).slice(-1)[0]}: ${d.msg}`).join("; ")
        : detail || `HTTP ${res.status}`;
      throw new Error(message);
    }
    return data;
  },
  get: (url) => api.request("GET", url),
  post: (url, body = {}) => api.request("POST", url, body),
  put: (url, body) => api.request("PUT", url, body),
  del: (url) => api.request("DELETE", url, {}), // the CSRF guard needs a JSON body on every write
};

function toast(message, ms = 2600) {
  const el = document.createElement("div");
  el.className = "toast";
  el.textContent = message;
  document.body.appendChild(el);
  setTimeout(() => el.remove(), ms);
}

// Run an API action from a button; reload the page on success.
async function act(button, fn, { reload = true, confirmText } = {}) {
  if (confirmText && !confirm(confirmText)) return;
  if (button) button.disabled = true;
  try {
    const out = await fn();
    if (reload) location.reload();
    return out;
  } catch (e) {
    toast(e.message, 5000);
  } finally {
    if (button) button.disabled = false;
  }
}

// Read named inputs of a form into an object; empty strings become null.
function formData(form) {
  const out = {};
  for (const el of form.elements) {
    if (!el.name) continue;
    let v = el.value.trim();
    if (v === "") v = null;
    else if (el.type === "number") v = Number(v);
    else if (el.dataset.json !== undefined) {
      try { v = JSON.parse(v); } catch { throw new Error(`${el.name}: invalid JSON`); }
    }
    out[el.name] = v;
  }
  return out;
}

async function copyText(button, text) {
  try {
    await navigator.clipboard.writeText(text.trim());
    toast("Copied");
  } catch {
    toast("Couldn't copy — select the text and copy it by hand", 4000);
  }
}

async function syncNow(button) {
  try {
    button.disabled = true;
    const data = await api.post("/api/sync", { full: true });
    if (data.status.startsWith("already_")) toast(data.message);
    activity.watch();
  } catch (e) {
    toast(e.message, 5000);
  } finally {
    button.disabled = false;
  }
}

// ⓘ tips: a popup placed over everything (cards clip overflow). Tap toggles on
// phones; hover shows it on desktop. Kept inside the viewport.
const tips = (() => {
  let pop = null, owner = null;
  const hide = () => { pop?.remove(); pop = null; owner = null; };
  const show = (tip) => {
    hide();
    owner = tip;
    pop = document.createElement("div");
    pop.className = "tip-pop";
    pop.textContent = tip.dataset.tip;
    document.body.appendChild(pop);
    const r = tip.getBoundingClientRect(), p = pop.getBoundingClientRect();
    const left = Math.min(Math.max(12, r.left + r.width / 2 - p.width / 2), window.innerWidth - p.width - 12);
    const top = r.top - p.height - 8 >= 8 ? r.top - p.height - 8 : r.bottom + 8;
    pop.style.left = `${left}px`;
    pop.style.top = `${top}px`;
  };
  document.addEventListener("click", (e) => {
    const tip = e.target.closest(".tip");
    if (!tip) return hide();
    e.preventDefault();
    e.stopPropagation();
    owner === tip ? hide() : show(tip);
  });
  document.addEventListener("mouseover", (e) => {
    const tip = e.target.closest?.(".tip");
    if (tip && matchMedia("(hover: hover)").matches) show(tip);
  });
  document.addEventListener("mouseout", (e) => { if (e.target.closest?.(".tip") && matchMedia("(hover: hover)").matches) hide(); });
  window.addEventListener("scroll", hide, { passive: true });
  return { hide };
})();

// PWA: installable app shell; the worker caches static assets only.
if ("serviceWorker" in navigator && location.protocol === "https:") {
  window.addEventListener("load", () => navigator.serviceWorker.register("/sw.js").catch(() => {}));
}

// ---------------------------------------------------------------------------
// Background work: a slim bar under the header says what's running (sync,
// grading, suggestions, Garmin uploads). When it finishes, the page refreshes
// once and keeps its scroll position — or, if you're typing or have a dialog
// open, offers the refresh instead of pulling the page from under you.
// ---------------------------------------------------------------------------

const activity = (() => {
  const LABELS = {
    sync: "Syncing with Garmin", sync_light: "Checking for last night's sleep", grade: "Grading",
    suggest: "Writing a suggestion", garmin_workout: "Updating your Garmin calendar", backup: "Backing up",
    vo2max_backfill: "Backfilling VO2max", decoupling_backfill: "Recomputing decoupling",
  };
  const NOUNS = { grade: ["session", "sessions"], garmin_workout: ["workout", "workouts"] };
  const SCROLL_KEY = `scroll:${location.pathname}${location.search}`;
  let bar = null, timer = null, seen = null, changed = false;

  const describe = (jobs) => {
    const counts = {};
    for (const j of jobs) counts[j.type] = (counts[j.type] || 0) + 1;
    return Object.entries(counts).map(([type, n]) => {
      const label = LABELS[type] || type.replace(/_/g, " ");
      const noun = NOUNS[type];
      return noun ? `${label} ${n} ${noun[n === 1 ? 0 : 1]}` : label;
    }).join(" · ");
  };

  const ensureBar = () => {
    if (bar) return bar;
    bar = document.createElement("div");
    bar.className = "activity-bar";
    bar.innerHTML = '<span class="spin"></span><span class="text"></span><button class="small secondary" hidden>Refresh</button>';
    bar.querySelector("button").onclick = () => refresh();
    document.querySelector("main")?.before(bar);
    return bar;
  };

  const busyUser = () => {
    const el = document.activeElement;
    return (el && ["INPUT", "TEXTAREA", "SELECT"].includes(el.tagName) && el.type !== "checkbox")
      || document.querySelector("dialog[open]") || location.pathname.startsWith("/chat");
  };

  function refresh() {
    try { sessionStorage.setItem(SCROLL_KEY, String(window.scrollY)); } catch {}
    location.reload();
  }

  async function poll() {
    let data;
    try { data = await api.get("/api/jobs/active"); } catch { timer = setTimeout(poll, 10000); return; }
    const ids = new Set(data.active.map((j) => j.id));
    if (seen && [...seen].some((id) => !ids.has(id))) changed = true;  // something finished
    seen = ids;
    if (data.active.length) {
      const b = ensureBar();
      b.classList.remove("done");
      b.querySelector(".text").textContent = `${describe(data.active)}…`;
      b.querySelector("button").hidden = true;
      timer = setTimeout(poll, 3000);
      return;
    }
    timer = null;
    if (!changed) { if (bar) bar.remove(), (bar = null); return; }
    changed = false;
    if (!busyUser()) return refresh();
    const b = ensureBar();
    b.classList.add("done");
    b.querySelector(".text").textContent = "Updated in the background.";
    b.querySelector("button").hidden = false;
  }

  function watch() {
    if (timer) clearTimeout(timer);
    timer = setTimeout(poll, 600);
  }

  window.addEventListener("load", () => {
    // Put the page back where it was before an automatic refresh.
    let y = null;
    try { y = sessionStorage.getItem(SCROLL_KEY); sessionStorage.removeItem(SCROLL_KEY); } catch {}
    if (y !== null) requestAnimationFrame(() => window.scrollTo(0, Number(y)));
    poll();
  });
  return { watch };
})();
