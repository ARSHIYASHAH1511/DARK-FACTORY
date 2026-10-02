(() => {
  "use strict";

  // ------------------------------------------------------------ helpers
  const $ = (s, r = document) => r.querySelector(s);
  const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const el = (html) => { const t = document.createElement("template"); t.innerHTML = html.trim(); return t.content.firstElementChild; };
  const TOKEN_KEY = "pf_token";
  const token = () => { try { return localStorage.getItem(TOKEN_KEY); } catch (e) { return null; } };

  function uuid() {
    const b = new Uint8Array(16);
    (window.crypto || window.msCrypto).getRandomValues(b);
    b[6] = (b[6] & 0x0f) | 0x40; b[8] = (b[8] & 0x3f) | 0x80;
    const h = Array.from(b, (x) => x.toString(16).padStart(2, "0")).join("");
    return `${h.slice(0, 8)}-${h.slice(8, 12)}-${h.slice(12, 16)}-${h.slice(16, 20)}-${h.slice(20)}`;
  }

  // One idempotency key per distinct request body: the key only changes when the body does.
  function keyed() {
    let last = null, key = null;
    return (fp) => { if (fp !== last) { last = fp; key = uuid(); } return key; };
  }

  let CUR = { minor: 2, code: "EUR" };
  let ME = null;
  let bootDone;
  const booted = new Promise((r) => { bootDone = r; });

  function fmt(n, withCode = true) {
    n = Math.trunc(Number(n));
    let s;
    if (CUR.minor === 0) s = String(n);
    else {
      const d = String(n).padStart(CUR.minor + 1, "0");
      s = d.slice(0, -CUR.minor) + "." + d.slice(-CUR.minor);
    }
    return withCode ? `${s} ${CUR.code}` : s;
  }

  // Decimal string -> minor units, or {error}
  function parseAmount(raw) {
    const s = String(raw == null ? "" : raw).trim();
    if (!/^[0-9]+(\.[0-9]+)?$/.test(s)) return { error: "Enter an amount as a number, like 15.00." };
    const [ip, fp = ""] = s.split(".");
    if (fp.length > CUR.minor) {
      return { error: CUR.minor === 0 ? `${CUR.code} has no decimal places.` : `At most ${CUR.minor} decimal places for ${CUR.code}.` };
    }
    if (ip.replace(/^0+/, "").length > 12) return { error: "That amount is too large." };
    const minor = Number(ip) * Math.pow(10, CUR.minor) + Number((fp + "0".repeat(CUR.minor)).slice(0, CUR.minor) || 0);
    if (minor < 1) return { error: "Amount must be greater than zero." };
    if (minor > 1000000000) return { error: "Amount is above the maximum of " + fmt(1000000000) + "." };
    return { minor };
  }

  const normHandle = (v) => String(v || "").trim().replace(/^@/, "");

  function fmtTime(iso) {
    const d = new Date(iso);
    if (isNaN(d)) return iso;
    return d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
  }

  function relExpiry(iso, status) {
    const ms = new Date(iso).getTime() - Date.now();
    if (status !== "open") return "";
    if (ms <= 0) return "expiring now";
    const m = Math.round(ms / 60000);
    if (m < 1) return "expires in under a minute";
    if (m < 90) return `expires in ${m} min`;
    const hrs = Math.round(m / 60);
    return hrs < 48 ? `expires in ${hrs} h` : `expires in ${Math.round(hrs / 24)} days`;
  }

  const FRIENDLY = {
    insufficient_funds: "Not enough available funds for that. Money on hold can't be spent.",
    not_found: "We couldn't find that person or item.",
    self_payment: "You can't send money to yourself.",
    self_request: "You can't request money from yourself.",
    request_not_pending: "That request is no longer pending.",
    authorization_not_open: "That authorization is no longer open.",
    authorization_expired: "That authorization has expired.",
    capture_exceeds_authorization: "That's more than what is still held.",
    forbidden: "You're not allowed to do that.",
    email_taken: "That email is already registered.",
    handle_taken: "That email maps to a handle that's already taken.",
    unauthenticated: "Those credentials didn't work.",
    idempotency_key_reuse: "That request was already used with different details. Change a field and retry.",
  };
  const errText = (r) => {
    const e = r && r.data && r.data.error;
    if (!e) return "Something went wrong (" + (r ? r.status : "?") + ").";
    return FRIENDLY[e.code] || e.message || e.code;
  };

  // ------------------------------------------------------------ http
  async function send(method, path, body, key) {
    const headers = { Accept: "application/json" };
    const t = token();
    if (t) headers.Authorization = "Bearer " + t;
    let payload;
    if (body !== undefined) { headers["Content-Type"] = "application/json"; payload = JSON.stringify(body); }
    if (key) headers["Idempotency-Key"] = key;
    let res, data = null;
    try {
      res = await fetch(path, { method, headers, body: payload, cache: "no-store" });
      const text = await res.text();
      if (text) { try { data = JSON.parse(text); } catch (e) { if (res.ok) return { lost: true }; } }
    } catch (e) {
      return { lost: true };
    }
    if (res.status === 401 && !path.startsWith("/auth/")) {
      try { localStorage.removeItem(TOKEN_KEY); } catch (e) { /* ignore */ }
      if (!/^\/(login|signup)$/.test(location.pathname)) { location.replace("/login"); }
    }
    return { status: res.status, ok: res.ok, data };
  }
  const get = (p) => send("GET", p);
  const uncertain = (r) => r.lost || r.status >= 500;

  // ------------------------------------------------------------ view bits
  const ICON = {
    home: '<path d="M3 11.5 12 4l9 7.5"/><path d="M5 10v10h14V10"/>',
    requests: '<path d="M4 6h16v12H4z"/><path d="m4 7 8 6 8-6"/>',
    split: '<circle cx="6" cy="6" r="2.5"/><circle cx="6" cy="18" r="2.5"/><path d="M8 7.5 20 17M8 16.5 20 7"/>',
    hold: '<rect x="4" y="10" width="16" height="10" rx="2.5"/><path d="M8 10V7a4 4 0 0 1 8 0v3"/>',
    login: '<path d="M10 17l5-5-5-5M15 12H3"/><path d="M14 4h6v16h-6"/>',
    signup: '<circle cx="9" cy="8" r="4"/><path d="M2 21c0-4 3-7 7-7s7 3 7 7M19 8v6M16 11h6"/>',
    out: '<path d="M7 17 17 7M9 7h8v8"/>',
    inn: '<path d="M17 7 7 17M15 17H7V9"/>',
    refresh: '<path d="M20 11a8 8 0 1 0-2.3 5.7M20 4v7h-7"/>',
  };
  const svg = (name, size = 22, extra = "") =>
    `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" ${extra}>${ICON[name]}</svg>`;

  function illustration(kind) {
    const wallet = '<rect x="30" y="52" width="100" height="62" rx="12" fill="#232323" stroke="#6366f1" stroke-width="2"/><path d="M30 70h100" stroke="#6366f1" stroke-width="2"/><circle cx="108" cy="92" r="6" fill="#6366f1"/>';
    const extras = {
      activity: '<circle cx="124" cy="40" r="14" fill="#6366f1" opacity=".25"/><circle cx="124" cy="40" r="8" fill="#818cf8"/><path d="M44 36h26M44 28h14" stroke="#525252" stroke-width="3" stroke-linecap="round"/>',
      requests: '<path d="M56 28h48" stroke="#525252" stroke-width="3" stroke-linecap="round"/><path d="m118 30 8 8-8 8" stroke="#818cf8" stroke-width="3" fill="none" stroke-linecap="round" stroke-linejoin="round"/>',
      holds: '<rect x="62" y="22" width="36" height="26" rx="13" fill="none" stroke="#818cf8" stroke-width="3"/><rect x="54" y="40" width="52" height="8" rx="4" fill="#818cf8" opacity=".35"/>',
    };
    return `<svg viewBox="0 0 160 130" width="160" height="130" fill="none" aria-hidden="true">${extras[kind] || ""}${wallet}</svg>`;
  }
  const empty = (tid, kind, title, text) =>
    `<div data-testid="${tid}" class="flex flex-col items-center px-4 py-8 text-center">${illustration(kind)}<p class="mt-3 text-lg font-bold">${esc(title)}</p><p class="muted mt-1 max-w-xs text-sm">${esc(text)}</p></div>`;
  const skeletonRows = (n = 3) => Array.from({ length: n }, () =>
    '<div class="row" aria-hidden="true"><div class="skeleton h-10 w-10 rounded-full"></div><div class="flex-1 space-y-2"><div class="skeleton h-4 w-2/3"></div><div class="skeleton h-3 w-1/3"></div></div><div class="skeleton h-5 w-20"></div></div>').join("");

  const field = (id, label, tid, o = {}) =>
    `<div class="field"><input id="${id}" data-testid="${tid}" type="${o.type || "text"}" placeholder=" " autocomplete="${o.ac || "off"}" ${o.mode ? `inputmode="${o.mode}"` : ""} ${o.value != null ? `value="${esc(o.value)}"` : ""} ${o.extra || ""}><label for="${id}">${esc(label)}</label></div>`;
  const visField = (id, tid) =>
    `<div class="field"><select id="${id}" data-testid="${tid}"><option value="public">Public</option><option value="private">Private</option></select><label for="${id}">Visibility</label></div>`;

  function setMsg(box, kind, tid, text) {
    box.innerHTML = "";
    if (!kind) return;
    const icon = kind === "success" ? "✓" : kind === "uncertain" ? "?" : "!";
    const role = kind === "success" ? "status" : "alert";
    const n = el(`<div role="${role}" data-testid="${tid}" class="alert alert-${kind}"><span aria-hidden="true" class="font-extrabold">${icon}</span><span>${esc(text)}</span></div>`);
    box.appendChild(n);
  }

  async function busy(btn, fn) {
    const label = btn.innerHTML;
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner" aria-hidden="true"></span><span>Working…</span>';
    try { return await fn(); } finally { btn.disabled = false; btn.innerHTML = label; }
  }

  // ------------------------------------------------------------ shell
  function renderShell() {
    const path = location.pathname;
    const links = ME
      ? [["/", "Home", "home"], ["/requests", "Requests", "requests"], ["/split", "Split", "split"], ["/authorizations", "Holds", "hold"]]
      : [["/login", "Log in", "login"], ["/signup", "Sign up", "signup"]];
    $("#nav").innerHTML =
      '<div class="mb-4 hidden items-center gap-2 px-2 py-2 md:flex"><span class="grid h-9 w-9 place-items-center rounded-xl bg-[#6366f1]">' +
      '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="3" y="6" width="18" height="14" rx="3"/><path d="M3 10h18M16 15h2"/></svg></span>' +
      '<span class="text-xl font-extrabold tracking-tight">Pocketful</span></div>' +
      links.map(([href, label, icon]) => `<a class="nav-link" href="${href}" ${path === href ? 'aria-current="page"' : ""}>${svg(icon)}<span>${label}</span></a>`).join("");
    const ub = $("#userbar");
    if (ME) {
      ub.innerHTML =
        `<span class="grid h-8 w-8 flex-none place-items-center rounded-full bg-[#6366f1]/20 text-sm font-bold text-indigo-200" aria-hidden="true">${esc((ME.display_name || "?").trim().charAt(0).toUpperCase())}</span>` +
        `<div data-testid="current-user" class="flex min-w-0 flex-col leading-tight"><span class="max-w-[8.5rem] truncate text-sm font-semibold sm:max-w-xs">${esc(ME.display_name)}</span>` +
        `<span class="max-w-[8.5rem] truncate text-xs text-neutral-400">@<span data-testid="current-handle">${esc(ME.handle)}</span></span></div>` +
        '<button type="button" data-testid="logout-button" class="btn-ghost ml-1 flex-none">Log out</button>';
      $("[data-testid=logout-button]", ub).addEventListener("click", () => {
        try { localStorage.removeItem(TOKEN_KEY); } catch (e) { /* ignore */ }
        location.assign("/login");
      });
    } else {
      ub.innerHTML = '<a href="/login" class="text-sm font-semibold">Log in</a><a href="/signup" class="btn-ghost">Sign up</a>';
    }
  }

  const pageHead = (title, sub) =>
    `<div class="mb-5"><h1 class="text-2xl font-extrabold tracking-tight md:text-3xl">${esc(title)}</h1>${sub ? `<p class="muted mt-1 text-sm">${esc(sub)}</p>` : ""}</div>`;

  // ------------------------------------------------------------ wallet
  function walletCard(withRefresh) {
    return `<section class="card" aria-labelledby="wallet-h"><div class="flex items-start justify-between gap-3"><h2 id="wallet-h" class="eyebrow">Available to spend</h2>` +
      (withRefresh ? `<button type="button" data-testid="wallet-refresh" class="btn-ghost -mt-1 flex-none">${svg("refresh", 16)}Refresh</button>` : "") +
      `</div><div id="wallet" aria-live="polite"><div class="mt-3 space-y-3" aria-hidden="true"><div class="skeleton h-12 w-3/4"></div><div class="skeleton h-4 w-1/2"></div></div></div></section>`;
  }

  function renderWallet(me) {
    const box = $("#wallet");
    if (!box) return;
    const prev = box.dataset.available;
    const held = me.held > 0
      ? `<div class="flex items-center justify-between gap-3 rounded-xl bg-amber-500/10 px-3 py-2"><span class="inline-flex items-center gap-2 text-sm text-amber-200">${svg("hold", 16)}On hold</span><span class="money text-base text-amber-200" data-testid="wallet-held" data-amount="${me.held}">${fmt(me.held)}</span></div>`
      : "";
    box.innerHTML =
      `<div class="mt-2 money money-hero text-white break-words${prev !== undefined && prev !== String(me.available) ? " flash-ok" : ""}" data-testid="wallet-available" data-amount="${me.available}">${fmt(me.available)}</div>` +
      `<div class="mt-4 space-y-2 text-sm"><div class="flex items-center justify-between gap-3 px-3"><span class="muted">Total balance</span><span class="money text-base text-neutral-300" data-testid="wallet-balance" data-amount="${me.total}">${fmt(me.total)}</span></div>${held}</div>`;
    box.dataset.available = String(me.available);
  }

  function applyMe(me) {
    ME = me;
    CUR = { minor: me.minor_units, code: me.currency };
  }

  let walletSeq = 0;
  async function refreshWallet() {
    const my = ++walletSeq;
    const r = await get("/me");
    if (my !== walletSeq || !r.ok) return;
    applyMe(r.data); renderWallet(r.data);
  }

  // ------------------------------------------------------------ pay / request / authorize forms
  function payFormHTML() {
    return `<section class="card" aria-labelledby="pay-h"><h2 id="pay-h" class="card-title">Send money</h2>
<form id="pay-form" novalidate class="space-y-3">
${field("pay-handle", "Recipient handle", "pay-handle", { ac: "off" })}
${field("pay-amount", "Amount", "pay-amount", { mode: "decimal" })}
${field("pay-note", "Note (optional)", "pay-note", { extra: 'maxlength="200"' })}
${visField("pay-visibility", "pay-visibility")}
<button type="submit" class="btn" data-testid="pay-submit">Send payment</button>
<div id="pay-msgs" aria-live="polite"></div>
</form></section>`;
  }

  function bindMoneyForm({ form, ids, prefix, endpoint, bodyOf, onOk, msgs, okText, onRefused }) {
    const keyFor = keyed();
    const input = (n) => $("#" + ids[n], form);
    const btn = $("[type=submit]", form);
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      await booted;
      setMsg(msgs, null);
      const amt = parseAmount(input("amount").value);
      const handle = normHandle(input("handle").value);
      if (!handle) { setMsg(msgs, "error", prefix + "-error", "Enter a recipient handle."); return; }
      if (amt.error) { setMsg(msgs, "error", prefix + "-error", amt.error); return; }
      const body = bodyOf(handle, amt.minor, input("note").value, input("visibility") ? input("visibility").value : undefined);
      const key = keyFor(JSON.stringify(body));
      const r = await busy(btn, () => send("POST", endpoint, body, key));
      if (uncertain(r)) {
        setMsg(msgs, "uncertain", prefix + "-uncertain", "We didn't get a response, so we can't tell whether this went through. Press the button again to retry safely — it will not be sent twice.");
        return;
      }
      if (r.ok) {
        setMsg(msgs, "success", prefix + "-success", okText(r.data, handle, amt.minor));
        if (onOk) onOk(r.data);
      } else {
        setMsg(msgs, "error", prefix + "-error", errText(r));
        if (onRefused) onRefused(r);
      }
    });
  }

  function mountPay(root) {
    const form = $("#pay-form", root);
    bindMoneyForm({
      form, prefix: "pay", endpoint: "/payments", msgs: $("#pay-msgs", form),
      ids: { handle: "pay-handle", amount: "pay-amount", note: "pay-note", visibility: "pay-visibility" },
      bodyOf: (h, a, n, v) => ({ to_handle: h, amount: a, note: n, visibility: v }),
      okText: (d, h, a) => `Sent ${fmt(a)} to @${h}.`,
      onOk: () => refreshHome(), onRefused: () => refreshHome(),
    });
  }

  function requestFormHTML() {
    return `<section class="card" aria-labelledby="req-h"><h2 id="req-h" class="card-title">Request money</h2>
<form id="request-form" novalidate class="space-y-3">
${field("request-handle", "Who should pay? (handle)", "request-handle")}
${field("request-amount", "Amount", "request-amount", { mode: "decimal" })}
${field("request-note", "Note (optional)", "request-note", { extra: 'maxlength="200"' })}
<button type="submit" class="btn" data-testid="request-submit">Send request</button>
<div id="request-msgs" aria-live="polite"></div>
</form></section>`;
  }

  function mountRequest(root) {
    const form = $("#request-form", root);
    bindMoneyForm({
      form, prefix: "request", endpoint: "/requests", msgs: $("#request-msgs", form),
      ids: { handle: "request-handle", amount: "request-amount", note: "request-note" },
      bodyOf: (h, a, n) => ({ payer_handle: h, amount: a, note: n }),
      okText: (d, h, a) => `Requested ${fmt(a)} from @${h}.`,
    });
  }

  function authorizeFormHTML() {
    return `<section class="card" aria-labelledby="auth-h"><h2 id="auth-h" class="card-title">Authorize a payment</h2>
<p class="muted -mt-2 mb-4 text-sm">Reserve money for someone to collect later. It's held, not spent, until they capture it.</p>
<form id="authorize-form" novalidate class="space-y-3">
${field("authorize-handle", "Recipient handle", "authorize-handle")}
${field("authorize-amount", "Amount to hold", "authorize-amount", { mode: "decimal" })}
${field("authorize-note", "Note (optional)", "authorize-note", { extra: 'maxlength="200"' })}
${visField("authorize-visibility", "authorize-visibility")}
<button type="submit" class="btn" data-testid="authorize-submit">Authorize</button>
<div id="authorize-msgs" aria-live="polite"></div>
</form></section>`;
  }

  function mountAuthorize(root, after) {
    const form = $("#authorize-form", root);
    bindMoneyForm({
      form, prefix: "authorize", endpoint: "/authorizations", msgs: $("#authorize-msgs", form),
      ids: { handle: "authorize-handle", amount: "authorize-amount", note: "authorize-note", visibility: "authorize-visibility" },
      bodyOf: (h, a, n, v) => ({ to_handle: h, amount: a, note: n, visibility: v }),
      okText: (d, h, a) => `Holding ${fmt(a)} for @${h}.`,
      onOk: after, onRefused: after,
    });
  }

  // ------------------------------------------------------------ home
  let homeSeq = 0;
  function renderFeed(payments) {
    const box = $("#feed");
    if (!box) return;
    if (!payments.length) {
      box.innerHTML = empty("empty-activity", "activity", "No activity yet", "Payments you send, receive or see publicly will show up here.");
      return;
    }
    const mine = ME ? ME.handle : "";
    const items = payments.map((p) => {
      const sent = p.from_handle === mine;
      const recv = p.to_handle === mine;
      const dir = sent ? "Sent" : recv ? "Received" : "Public";
      return `<li data-testid="activity-item-${esc(p.payment_id)}" data-visibility="${esc(p.visibility)}" class="row">
<span class="icon-bubble ${recv ? "icon-in" : ""}">${svg(recv ? "inn" : "out")}</span>
<div class="min-w-0 flex-1"><p class="truncate font-semibold"><span data-testid="activity-parties-${esc(p.payment_id)}">${esc(p.from_handle)} → ${esc(p.to_handle)}</span></p>
<p data-testid="activity-note-${esc(p.payment_id)}" class="muted break-words text-sm">${esc(p.note)}</p>
<p class="mt-1 flex flex-wrap items-center gap-2 text-xs text-neutral-400"><span class="chip ${recv ? "chip-ok" : "chip-neutral"}">${dir}</span><span class="chip chip-neutral">${p.visibility === "private" ? "🔒 Private" : "Public"}</span><time datetime="${esc(p.created_at)}">${esc(fmtTime(p.created_at))}</time>${p.authorization_id ? '<span class="chip chip-open">Captured hold</span>' : ""}</p></div>
<span data-testid="activity-amount-${esc(p.payment_id)}" class="money flex-none text-right text-base ${recv ? "text-emerald-300" : "text-white"}">${fmt(p.amount)}</span></li>`;
    }).join("");
    box.innerHTML = `<ul data-testid="activity-list" class="m-0 list-none p-0">${items}</ul>`;
  }

  async function refreshHome() {
    const my = ++homeSeq;
    walletSeq++;
    const wallet = $("#wallet");
    if (wallet) wallet.style.opacity = ".6";
    const [me, act] = await Promise.all([get("/me"), get("/activity?limit=50")]);
    if (my !== homeSeq) return;
    if (wallet) wallet.style.opacity = "";
    if (me.ok) { applyMe(me.data); renderWallet(me.data); }
    if (act.ok) renderFeed(act.data.payments);
  }

  function pageHome(root) {
    root.innerHTML = pageHead("Wallet", "Your money, at a glance.") +
      `<div class="grid gap-4 lg:grid-cols-2"><div class="space-y-4 lg:col-span-2">${walletCard(true)}</div>
<div class="space-y-4">${payFormHTML()}${requestFormHTML()}${authorizeFormHTML()}</div>
<section class="card self-start" aria-labelledby="feed-h"><h2 id="feed-h" class="card-title">Activity</h2><div id="feed" aria-live="polite">${skeletonRows(3)}</div></section></div>`;
    mountPay(root); mountRequest(root); mountAuthorize(root, () => refreshHome());
    $("[data-testid=wallet-refresh]", root).addEventListener("click", (e) => {
      const b = e.currentTarget; b.disabled = true;
      refreshHome().finally(() => { b.disabled = false; });
    });
    refreshHome();
  }

  // ------------------------------------------------------------ requests
  const reqPayKeys = new Map();
  async function pageRequests(root) {
    root.innerHTML = pageHead("Requests", "Money people asked you for, and money you asked others for.") +
      '<div id="req-msgs" aria-live="polite"></div><div id="req-body" class="space-y-4">' + `<div class="card">${skeletonRows(2)}</div></div>`;
    const msgs = $("#req-msgs", root);
    const body = $("#req-body", root);

    async function load() {
      const [inc, out] = await Promise.all([get("/requests?direction=incoming&limit=200"), get("/requests?direction=outgoing&limit=200")]);
      if (!inc.ok || !out.ok) return;
      draw(inc.data.requests, out.data.requests);
    }

    function item(r, incoming) {
      const id = esc(r.request_id);
      const who = incoming ? r.requester_handle : r.payer_handle;
      const pending = r.status === "pending";
      return `<li data-testid="request-item-${id}" data-status="${esc(r.status)}" class="row">
<span class="icon-bubble ${incoming ? "" : "icon-in"}">${svg(incoming ? "inn" : "out")}</span>
<div class="min-w-0 flex-1"><p class="truncate font-semibold">${incoming ? "From" : "To"} @${esc(who)}</p>
<p class="muted break-words text-sm">${esc(r.note)}</p>
<p class="mt-1 flex flex-wrap items-center gap-2 text-xs text-neutral-400"><span class="chip chip-${esc(r.status)}">${esc(r.status)}</span><time datetime="${esc(r.created_at)}">${esc(fmtTime(r.created_at))}</time></p>
${pending && incoming ? `<div class="mt-3 flex flex-col gap-2 sm:flex-row"><button type="button" class="btn btn-sm" data-act="pay" data-id="${id}" data-testid="request-pay-${id}">Pay ${fmt(r.amount)}</button><button type="button" class="btn-ghost" data-act="decline" data-id="${id}" data-testid="request-decline-${id}">Decline</button></div>` : ""}
${pending && !incoming ? `<div class="mt-3"><button type="button" class="btn-ghost btn-danger" data-act="cancel" data-id="${id}" data-testid="request-cancel-${id}">Cancel request</button></div>` : ""}
</div><span data-testid="request-amount-${id}" class="money flex-none text-right text-base">${fmt(r.amount)}</span></li>`;
    }

    function section(title, tid, list, incoming, hide) {
      return `<section class="card ${hide ? "hidden" : ""}"><h2 class="card-title">${title}</h2><ul data-testid="${tid}" class="m-0 list-none p-0">${list.length ? list.map((r) => item(r, incoming)).join("") : '<li class="muted py-2 text-sm">Nothing here yet.</li>'}</ul></section>`;
    }

    function draw(inc, out) {
      const none = !inc.length && !out.length;
      body.innerHTML = (none ? `<div class="card">${empty("empty-requests", "requests", "No requests yet", "Ask a friend for money, or split a bill, and requests appear here.")}</div>` : "") +
        section("Requests to you", "incoming-list", inc, true, none) + section("Requests you sent", "outgoing-list", out, false, none);
    }

    body.addEventListener("click", async (e) => {
      const b = e.target.closest("button[data-act]");
      if (!b) return;
      const id = b.dataset.id, act = b.dataset.act;
      setMsg(msgs, null);
      await booted;
      let key;
      if (act === "pay") { if (!reqPayKeys.has(id)) reqPayKeys.set(id, uuid()); key = reqPayKeys.get(id); }
      const r = await busy(b, () => send("POST", `/requests/${encodeURIComponent(id)}/${act}`, {}, key));
      if (uncertain(r)) {
        setMsg(msgs, "uncertain", "request-uncertain", "We didn't get a response, so we can't tell whether that went through. Try again — it won't be applied twice.");
        return;
      }
      if (!r.ok) setMsg(msgs, "error", "request-error", errText(r));
      else if (act === "pay") reqPayKeys.delete(id);
      await load();
    });
    await load();
  }

  // ------------------------------------------------------------ split
  function splitShares(amount, n) {
    const base = Math.floor(amount / n), rem = amount - base * n;
    return Array.from({ length: n }, (_, i) => base + (i < rem ? 1 : 0));
  }

  function pageSplit(root) {
    root.innerHTML = pageHead("Split a bill", "Everyone listed gets a fair share; the others are sent a request.") +
      `<section class="card max-w-xl"><form id="split-form" novalidate class="space-y-3">
${field("split-amount", "Total amount", "split-amount", { mode: "decimal" })}
${field("split-handles", "Handles, comma separated (include yourself)", "split-handles")}
${field("split-note", "Note (optional)", "split-note", { extra: 'maxlength="200"' })}
<div id="split-preview-box" aria-live="polite"></div>
<button type="submit" class="btn" data-testid="split-submit">Split it</button>
<div id="split-msgs" aria-live="polite"></div></form></section>`;
    const form = $("#split-form", root);
    const amountEl = $("#split-amount"), handlesEl = $("#split-handles"), noteEl = $("#split-note");
    const preview = $("#split-preview-box"), msgs = $("#split-msgs");
    const keyFor = keyed();
    const handles = () => handlesEl.value.split(",").map(normHandle).filter((h) => h);

    function drawPreview() {
      const amt = parseAmount(amountEl.value), hs = handles();
      if (amt.error || !hs.length || new Set(hs).size !== hs.length) { preview.innerHTML = ""; return; }
      const shares = splitShares(amt.minor, hs.length);
      preview.innerHTML = `<div data-testid="split-preview" class="rounded-xl border border-indigo-400/30 bg-indigo-500/10 p-3"><p class="eyebrow mb-2">Preview</p><ul class="m-0 list-none space-y-1 p-0">` +
        hs.map((h, i) => `<li class="flex items-center justify-between gap-3"><span class="truncate">@${esc(h)}</span><span class="money" data-testid="split-share-${esc(h)}">${fmt(shares[i])}</span></li>`).join("") + "</ul></div>";
    }
    [amountEl, handlesEl].forEach((i) => i.addEventListener("input", drawPreview));

    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      await booted;
      setMsg(msgs, null);
      const amt = parseAmount(amountEl.value), hs = handles();
      if (amt.error) { setMsg(msgs, "error", "split-error", amt.error); return; }
      if (!hs.length) { setMsg(msgs, "error", "split-error", "List at least one handle."); return; }
      if (new Set(hs).size !== hs.length) { setMsg(msgs, "error", "split-error", "Each handle can appear only once."); return; }
      drawPreview();
      const body = { amount: amt.minor, participant_handles: hs, note: noteEl.value };
      const key = keyFor(JSON.stringify(body));
      const r = await busy($("[type=submit]", form), () => send("POST", "/splits", body, key));
      if (uncertain(r)) {
        setMsg(msgs, "uncertain", "split-uncertain", "We didn't get a response, so we can't tell whether the split went through. Press the button again to retry safely.");
      } else if (r.ok) {
        const n = r.data.requests.length;
        setMsg(msgs, "success", "split-success", `Split ${fmt(amt.minor)} between ${hs.length}. ${n} request${n === 1 ? "" : "s"} sent.`);
      } else setMsg(msgs, "error", "split-error", errText(r));
    });
  }

  // ------------------------------------------------------------ authorizations
  const capKeys = new Map();
  async function pageAuthorizations(root) {
    root.innerHTML = pageHead("Authorizations", "Holds reserve money without moving it. Capture collects it; void releases it.") +
      `<div class="grid gap-4 lg:grid-cols-2"><div class="space-y-4">${walletCard(false)}${authorizeFormHTML()}</div>
<section class="card self-start" aria-labelledby="al-h"><h2 id="al-h" class="card-title">Your holds</h2><div id="auth-msgs" aria-live="polite"></div><div id="auth-body">${skeletonRows(3)}</div></section></div>`;
    const msgs = $("#auth-msgs", root), body = $("#auth-body", root);
    let seq = 0;

    async function load() {
      const my = ++seq;
      const [list, me] = await Promise.all([get("/authorizations?limit=200"), get("/me")]);
      if (my !== seq) return;
      if (me.ok) { applyMe(me.data); renderWallet(me.data); }
      if (list.ok) draw(list.data.authorizations);
    }

    function item(a) {
      const id = esc(a.authorization_id);
      const incoming = ME && a.to_user_id === ME.user_id;
      const open = a.status === "open";
      const rel = relExpiry(a.expires_at, a.status);
      return `<li data-testid="authorization-item-${id}" data-status="${esc(a.status)}" class="row">
<span class="icon-bubble ${incoming ? "icon-in" : ""}">${svg("hold")}</span>
<div class="min-w-0 flex-1"><p class="truncate font-semibold">${incoming ? "From" : "For"} @${esc(incoming ? a.from_handle : a.to_handle)}</p>
<p class="muted break-words text-sm">${esc(a.note)}</p>
<p class="mt-1 flex flex-wrap items-center gap-2 text-xs text-neutral-400"><span class="chip chip-${esc(a.status)}">${esc(a.status)}</span><span class="chip chip-neutral">${a.visibility === "private" ? "🔒 Private" : "Public"}</span><span class="chip chip-neutral">${incoming ? "Incoming" : "Outgoing"}</span></p>
<p class="mt-2 text-xs text-neutral-400"><span>Expires </span><time data-testid="authorization-expires-${id}" datetime="${esc(a.expires_at)}">${esc(a.expires_at)}</time>${rel ? ` · ${esc(rel)}` : ""}</p>
${a.status === "captured" ? `<p class="mt-1 text-sm text-emerald-300">Captured <span class="money" data-testid="authorization-captured-${id}">${fmt(a.captured_amount)}</span></p>` : (a.captured_amount > 0 ? `<p class="mt-1 text-xs text-neutral-400">${fmt(a.captured_amount)} captured so far</p>` : "")}
${open && incoming ? `<div class="mt-3 space-y-2"><div class="field max-w-xs"><input id="cap-${id}" data-testid="authorization-capture-amount-${id}" type="text" inputmode="decimal" placeholder=" " autocomplete="off" value="${fmt(a.remaining_amount, false)}"><label for="cap-${id}">Capture amount</label></div>
<label class="flex items-center gap-2 text-sm text-neutral-300"><input type="checkbox" id="keep-${id}" class="h-4 w-4 accent-indigo-500">Keep the rest on hold</label>
<button type="button" class="btn btn-sm" data-act="capture" data-id="${id}" data-testid="authorization-capture-${id}">Capture</button></div>` : ""}
${open && !incoming ? `<div class="mt-3"><button type="button" class="btn-ghost btn-danger" data-act="void" data-id="${id}" data-testid="authorization-void-${id}">Void hold</button></div>` : ""}
</div><span data-testid="authorization-amount-${id}" class="money flex-none text-right text-base">${fmt(a.amount)}</span></li>`;
    }

    function draw(list) {
      body.innerHTML = `<ul data-testid="authorization-list" class="m-0 list-none p-0">${list.map(item).join("")}</ul>` +
        (list.length ? "" : empty("empty-authorizations", "holds", "No holds yet", "Authorize a payment to reserve money for someone to collect later."));
    }

    body.addEventListener("click", async (e) => {
      const b = e.target.closest("button[data-act]");
      if (!b) return;
      const id = b.dataset.id, act = b.dataset.act;
      setMsg(msgs, null);
      await booted;
      let r;
      if (act === "capture") {
        const amt = parseAmount($(`#cap-${CSS.escape(id)}`).value);
        if (amt.error) { setMsg(msgs, "error", "authorization-error", amt.error); return; }
        const keep = $(`#keep-${CSS.escape(id)}`).checked;
        const payload = keep ? { amount: amt.minor, final: false } : { amount: amt.minor };
        const fp = id + JSON.stringify(payload);
        if (!capKeys.has(fp)) capKeys.set(fp, uuid());
        r = await busy(b, () => send("POST", `/authorizations/${encodeURIComponent(id)}/capture`, payload, capKeys.get(fp)));
      } else {
        r = await busy(b, () => send("POST", `/authorizations/${encodeURIComponent(id)}/void`, {}));
      }
      if (uncertain(r)) {
        setMsg(msgs, "uncertain", "authorization-uncertain", "We didn't get a response, so we can't tell whether that went through. Try again — captures are never applied twice.");
        return;
      }
      if (!r.ok) setMsg(msgs, "error", "authorization-error", errText(r));
      await load();
    });

    mountAuthorize(root, () => load());
    await load();
  }

  // ------------------------------------------------------------ auth screens
  function authPage(root, kind) {
    const signup = kind === "signup";
    root.innerHTML = `<div class="mx-auto max-w-md"><div class="mb-6 text-center">${illustration("holds")}<h1 class="mt-2 text-2xl font-extrabold tracking-tight">${signup ? "Create your wallet" : "Welcome back"}</h1>
<p class="muted mt-1 text-sm">${signup ? "Sign up to send, request and split money." : "Log in to your Pocketful wallet."}</p></div>
<section class="card"><form id="auth-form" novalidate class="space-y-3">
${signup ? field("signup-display-name", "Display name", "signup-display-name", { ac: "name" }) : ""}
${field(kind + "-email", "Email", kind + "-email", { type: "email", ac: "email" })}
${field(kind + "-password", "Password", kind + "-password", { type: "password", ac: signup ? "new-password" : "current-password" })}
<button type="submit" class="btn" data-testid="${kind}-submit">${signup ? "Create account" : "Log in"}</button>
<div id="auth-msgs" aria-live="polite"></div></form>
<p class="muted mt-4 text-center text-sm">${signup ? 'Already have an account? <a href="/login">Log in</a>' : 'New here? <a href="/signup">Sign up</a>'}</p></section></div>`;
    const form = $("#auth-form", root), msgs = $("#auth-msgs", root);
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      setMsg(msgs, null);
      const body = { email: $("#" + kind + "-email").value.trim(), password: $("#" + kind + "-password").value };
      if (signup) body.display_name = $("#signup-display-name").value;
      const r = await busy($("[type=submit]", form), () => send("POST", signup ? "/auth/signup" : "/auth/login", body));
      if (r.lost) { setMsg(msgs, "error", "auth-error", "Couldn't reach the server. Check your connection and try again."); return; }
      if (!r.ok) { setMsg(msgs, "error", "auth-error", errText(r)); return; }
      try { localStorage.setItem(TOKEN_KEY, r.data.token); } catch (e2) { /* ignore */ }
      location.assign("/");
    });
  }

  // ------------------------------------------------------------ boot
  async function boot() {
    const path = location.pathname;
    const root = $("#app");
    const authScreen = path === "/login" || path === "/signup";
    if (token()) {
      const r = await get("/me");
      if (r.ok) applyMe(r.data);
      else if (r.status === 401) { try { localStorage.removeItem(TOKEN_KEY); } catch (e) { /* ignore */ } }
    }
    renderShell();
    bootDone();
    if (!ME && !authScreen) {
      if (token()) { root.innerHTML = '<div class="card">' + skeletonRows(2) + "</div>"; return; }
      location.replace("/login");
      return;
    }
    if (path === "/login") authPage(root, "login");
    else if (path === "/signup") authPage(root, "signup");
    else if (path === "/requests") pageRequests(root);
    else if (path === "/split") pageSplit(root);
    else if (path === "/authorizations") pageAuthorizations(root);
    else pageHome(root);
  }
  boot();
})();
