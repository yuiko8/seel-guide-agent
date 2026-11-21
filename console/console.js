// Atlas ops console: traces, breakers & chaos, tickets, badcase labels.
// Plain ES module, no build step. All data comes from /api; nothing is cached.

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const CATEGORIES = ["routing", "slots", "retrieval", "generation", "guard", "tool", "harness", "ux", "other"];
const KINDS = ["node", "llm", "tool", "retrieval", "guard", "degrade", "context"];
const POLL_MS = 4000;

const ui = {
  tab: "traces", only: "all", q: "", session: null, selected: null, openSpans: new Set(),
  health: null, token: load("ops_token") || "", tokenPrompted: false,
};

function load(k) { try { return localStorage.getItem(k); } catch { return null; } }
function save(k, v) { try { v ? localStorage.setItem(k, v) : localStorage.removeItem(k); } catch { /* private mode */ } }

// ---------------------------------------------------------------- api
async function api(path, opts = {}) {
  const headers = { ...(opts.body ? { "Content-Type": "application/json" } : {}), ...(ui.token ? { "X-Admin-Token": ui.token } : {}) };
  const r = await fetch(path, { ...opts, headers, body: opts.body ? JSON.stringify(opts.body) : undefined });
  if (r.status === 401) { askToken(); throw new Error("Admin token required"); }
  if (!r.ok) {
    let msg = `${r.status}`;
    try { msg = (await r.json()).detail || msg; } catch { /* not json */ }
    throw new Error(typeof msg === "string" ? msg : JSON.stringify(msg));
  }
  return opts.raw ? r : r.json();
}

function banner(msg) {
  const b = $("#banner");
  b.hidden = !msg;
  b.textContent = msg || "";
}

function askToken() {
  if (ui.tokenPrompted) return;
  ui.tokenPrompted = true;
  $("#token-btn").hidden = false;
  const d = $("#token-dialog");
  $("#token-input").value = ui.token;
  d.showModal();
}
$("#token-dialog").addEventListener("close", () => {
  ui.tokenPrompted = false;
  if ($("#token-dialog").returnValue === "ok") {
    ui.token = $("#token-input").value.trim();
    save("ops_token", ui.token);
    refresh();
  }
});
$("#token-btn").addEventListener("click", askToken);

// ---------------------------------------------------------------- formatting
const ms = (v) => (v >= 1000 ? `${(v / 1000).toFixed(2)}s` : `${Math.round(v)}ms`);
const usd = (v) => (v >= 0.01 ? `$${v.toFixed(3)}` : v > 0 ? `$${v.toFixed(5)}` : "$0");
const pct = (v) => `${(v * 100).toFixed(v > 0 && v < 0.1 ? 1 : 0)}%`;
const ago = (ts) => {
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 60) return `${Math.round(s)}s ago`;
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  if (s < 86400) return `${Math.round(s / 3600)}h ago`;
  return new Date(ts * 1000).toLocaleDateString();
};
const clock = (ts) => new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23" });

function labelPill(l) {
  if (!l) return "";
  return l.verdict === "good" ? `<span class="pill ok">good</span>` : `<span class="pill bad">bad · ${esc(l.category || "other")}</span>`;
}

// ---------------------------------------------------------------- tabs
$$(".tabs button").forEach((b) => b.addEventListener("click", () => setTab(b.dataset.tab)));
function setTab(tab) {
  ui.tab = tab;
  $$(".tabs button").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.tab === tab)));
  $$(".view").forEach((v) => (v.hidden = v.id !== `view-${tab}`));
  try { history.replaceState(null, "", `#${tab}`); } catch { /* sandboxed */ }
  refresh();
}

// ---------------------------------------------------------------- health header
async function loadHealth() {
  const h = await api("/api/health");
  ui.health = h;
  $("#env-line").textContent = `${h.llm_mode} · ${h.models.join(" → ")} · prompt ${h.prompt_version}`;
  $("#token-btn").hidden = !h.admin_auth;
  const faults = Object.keys(h.faults || {});
  const open = (h.breakers || []).filter((b) => b.state !== "closed").map((b) => b.name);
  const parts = [];
  if (faults.length) parts.push(`Chaos active on ${faults.join(", ")}`);
  if (open.length) parts.push(`Breaker not closed: ${open.join(", ")}`);
  if (!h.chaos_enabled) parts.push("Chaos controls are disabled on this server (ADMIN_CHAOS=0)");
  banner(parts.join(" · "));
  return h;
}

// ---------------------------------------------------------------- traces
$$("#filter button").forEach((b) => b.addEventListener("click", () => {
  ui.only = b.dataset.only;
  $$("#filter button").forEach((x) => x.setAttribute("aria-pressed", String(x === b)));
  loadTraces();
}));
let qTimer;
$("#q").addEventListener("input", (e) => {
  clearTimeout(qTimer);
  qTimer = setTimeout(() => { ui.q = e.target.value.trim(); loadTraces(); }, 250);
});

function setSession(sid) {
  ui.session = sid;
  const el = $("#session-filter");
  el.hidden = !sid;
  el.innerHTML = sid ? `Session <code>${esc(sid)}</code> <button class="btn ghost sm" id="clear-session">Show all sessions</button>` : "";
  if (sid) $("#clear-session").addEventListener("click", () => setSession(null));
  loadTraces();
}

async function loadStats() {
  const s = await api("/api/stats?window=200");
  const tiles = [
    ["Turns", s.window, `${s.sessions} sessions`],
    ["Degraded", pct(s.degraded_rate)],
    ["Errors", pct(s.error_rate)],
    ["Guard blocks", pct(s.guard_block_rate)],
    ["Retries", pct(s.retry_rate)],
    ["Latency p50/p95", `${ms(s.latency_ms.p50)}`, ms(s.latency_ms.p95)],
    ["Cost / turn", usd(s.cost_usd.per_turn), `${usd(s.cost_usd.total)} total`],
    ["Labels", `${s.labels.bad} bad`, `${s.labels.good} good`],
  ];
  $("#stats").innerHTML = tiles.map(([k, v, sub]) =>
    `<div class="stat"><div class="stat-k">${esc(k)}</div><div class="stat-v">${esc(v)}${sub !== undefined ? ` <small>${esc(sub)}</small>` : ""}</div></div>`).join("");
}

async function loadTraces() {
  const p = new URLSearchParams({ limit: "100", only: ui.only });
  if (ui.q) p.set("q", ui.q);
  if (ui.session) p.set("session_id", ui.session);
  const rows = await api(`/api/traces?${p}`);
  const list = $("#trace-list");
  if (!rows.length) {
    list.innerHTML = `<div class="empty">No turns${ui.only !== "all" ? ` matching “${esc(ui.only)}”` : ""} yet. Chat in the <a href="/" target="_blank" rel="noopener">shopper app</a> to create some.</div>`;
    return;
  }
  list.innerHTML = rows.map((t) => {
    const tags = [
      t.intent ? `<span class="pill info">${esc(t.intent)}</span>` : "",
      ...t.degradations.map((d) => `<span class="pill warn">${esc(d)}</span>`),
      t.errors ? `<span class="pill bad">${t.errors} error${t.errors > 1 ? "s" : ""}</span>` : "",
      t.guard_blocks ? `<span class="pill bad">guard ×${t.guard_blocks}</span>` : "",
      t.retries ? `<span class="pill">retry ×${t.retries}</span>` : "",
      labelPill(t.label),
    ].join("");
    return `<button class="item${t.trace_id === ui.selected ? " sel" : ""}" data-id="${esc(t.trace_id)}">
      <div class="item-top"><span>${esc(t.session_id)} · turn ${t.turn}</span><span>${ms(t.duration_ms)} · ${ago(t.started_at)}</span></div>
      <div class="item-msg">${esc(t.user_message)}</div>
      <div class="item-reply">${esc(t.reply)}</div>
      <div class="item-tags">${tags}</div>
    </button>`;
  }).join("");
  $$(".item", list).forEach((b) => b.addEventListener("click", () => openTrace(b.dataset.id)));
}

async function openTrace(id, keepOpenSpans = false) {
  if (!keepOpenSpans || id !== ui.selected) ui.openSpans = new Set();
  ui.selected = id;
  $$(".item").forEach((b) => b.classList.toggle("sel", b.dataset.id === id));
  const d = await api(`/api/traces/${encodeURIComponent(id)}`);
  renderDetail(d);
}

function closeDetail() {
  ui.selected = null;
  $("#detail").hidden = true;
  $(".split").classList.remove("has-detail");
  document.body.classList.remove("sheet-open");
  $$(".item").forEach((b) => b.classList.remove("sel"));
}

function renderDetail(d) {
  const tr = d.trace, resp = d.response || {};
  const el = $("#detail");
  el.hidden = false;
  $(".split").classList.add("has-detail");
  if (matchMedia("(max-width: 979px)").matches) document.body.classList.add("sheet-open");

  const blocks = (resp.messages || []).filter((m) => m.type !== "text").map((m) =>
    m.type === "product_cards" ? `product_cards ×${m.items.length}` : m.type === "chips" ? `chips ×${m.options.length}` :
    m.type === "sources" ? `sources ×${m.items.length}` : m.type);
  const slots = Object.entries(resp.slots || {}).map(([k, v]) => `${k}=${Array.isArray(v) ? v.join("/") : v}`).join(", ");
  const u = tr.usage || {};
  const path = (tr.path || []).map((n) => `<span class="pill">${esc(n)}</span>`).join(`<span class="arrow">→</span>`);

  el.innerHTML = `
    <div class="detail-head">
      <button class="btn ghost sm" id="close-detail" aria-label="Close trace">← Back</button>
      <span class="mono">${esc(tr.trace_id)}</span>
    </div>
    <div class="convo">
      <div class="bub user"><div class="bub-k">User · turn ${d.turn}</div>${esc(d.user_message)}</div>
      <div class="bub bot"><div class="bub-k">Agent${resp.intent ? ` · ${esc(resp.intent)}` : ""}</div>${esc(((resp.messages || []).find((m) => m.type === "text") || {}).text || "")}</div>
    </div>
    <dl class="kv">
      <dt>Path</dt><dd><div class="path">${path}</div></dd>
      <dt>Degraded</dt><dd>${tr.degradations.length ? tr.degradations.map((x) => `<span class="pill warn">${esc(x)}</span>`).join(" ") : "none"}</dd>
      <dt>Latency</dt><dd>${ms(tr.duration_ms)}</dd>
      <dt>LLM</dt><dd>${u.llm_calls || 0} calls · ${u.input_tokens || 0} in / ${u.output_tokens || 0} out tokens · ${usd(u.cost_usd || 0)}</dd>
      <dt>Slots</dt><dd>${esc(slots || "—")}</dd>
      <dt>UI blocks</dt><dd>${esc(blocks.join(", ") || "text only")}</dd>
      <dt>Version</dt><dd>prompt ${esc(tr.meta?.prompt_version || "?")} · ${esc(tr.meta?.llm_mode || "?")}</dd>
      <dt>Session</dt><dd><code>${esc(d.session_id)}</code> <button class="btn ghost sm" id="filter-session">All turns</button></dd>
      <dt>Request</dt><dd><code>${esc(tr.request_id)}</code></dd>
    </dl>
    <h3>Spans</h3>
    <div class="legend">${KINDS.map((k) => `<span><i class="dot k-${k}"></i>${k}</span>`).join("")}<span><i class="dot is-err"></i>error</span></div>
    <div class="wf" id="wf">${waterfall(tr)}</div>
    ${labelForm(d)}
  `;
  $("#close-detail").addEventListener("click", closeDetail);
  $("#filter-session").addEventListener("click", () => { closeDetail(); setSession(d.session_id); });
  wireWaterfall(tr);
  wireLabelForm(d);
}

function wireWaterfall(tr) {
  $$("#wf .wf-row").forEach((row) => row.addEventListener("click", () => {
    const i = Number(row.dataset.i);
    ui.openSpans.has(i) ? ui.openSpans.delete(i) : ui.openSpans.add(i);
    $("#wf").innerHTML = waterfall(tr);
    wireWaterfall(tr);
  }));
}

function spanLabel(s) {
  const a = s.attrs || {};
  const bits = [];
  if (a.task) bits.push(a.task);
  if (s.kind === "llm" && a.input_tokens !== undefined) bits.push(`${a.input_tokens}→${a.output_tokens} tok`);
  if (a.attempts && a.attempts.length > 1) bits.push(`${a.attempts.length} attempts`);
  if (a.idempotent_replay) bits.push("replay");
  if (a.violations && a.violations.length) bits.push(`${a.violations.length} violation${a.violations.length > 1 ? "s" : ""}`);
  if (s.kind === "degrade" && a.reason) bits.push(a.reason);
  if (a.error) bits.push(a.error.split(":")[0]);
  return bits.length ? ` <span class="mono" style="color:var(--muted)">${esc(bits.join(" · "))}</span>` : "";
}

function waterfall(tr) {
  const total = Math.max(tr.duration_ms || 0, ...tr.spans.map((s) => s.start_ms + s.duration_ms), 1);
  return tr.spans.map((s, i) => {
    const kind = KINDS.includes(s.kind) ? s.kind : "context";
    const left = (s.start_ms / total) * 100;
    const width = Math.max((s.duration_ms / total) * 100, 0.6);
    const err = s.status === "error" || s.status === "blocked";
    const open = ui.openSpans.has(i);
    const attempts = (s.attrs?.attempts || []).map((a) =>
      `<span class="pill${a.error ? " bad" : " ok"}">#${a.n + 1} ${a.ms}ms${a.error ? ` ${esc(a.error)}` : ""}${a.backoff_s ? ` · wait ${a.backoff_s}s` : ""}</span>`).join("");
    return `<button class="wf-row status-${esc(s.status)}${open ? " open" : ""}" data-i="${i}" aria-expanded="${open}">
        <span class="wf-name"><i class="dot k-${kind}${err ? " is-err" : ""}"></i><span>${esc(s.name)}${spanLabel(s)}</span></span>
        <span class="wf-track"><span class="wf-bar k-${kind}${err ? " is-err" : ""}" style="left:${left}%;width:${width}%"></span></span>
        <span class="wf-ms">${s.duration_ms ? ms(s.duration_ms) : "·"}</span>
      </button>${open ? `<div class="wf-attrs">${attempts ? `<div class="attempts">${attempts}</div>` : ""}<pre>${esc(JSON.stringify({ kind: s.kind, status: s.status, start_ms: s.start_ms, ...s.attrs }, null, 2))}</pre></div>` : ""}`;
  }).join("");
}

// ---------------------------------------------------------------- label form
function labelForm(d) {
  const l = d.label || {};
  const intents = ui.health?.intents || [];
  return `<form class="label-form" id="label-form">
    <h3>Label this turn</h3>
    <div class="seg" role="group" aria-label="Verdict">
      <button type="button" data-v="good" aria-pressed="${l.verdict === "good"}">Good</button>
      <button type="button" data-v="bad" aria-pressed="${l.verdict === "bad"}">Bad</button>
    </div>
    <div class="grid2" id="bad-fields" ${l.verdict === "bad" ? "" : "hidden"}>
      <label class="field">Failing layer
        <select name="category">${CATEGORIES.map((c) => `<option ${c === (l.category || "other") ? "selected" : ""}>${c}</option>`).join("")}</select></label>
      <label class="field">Expected intent
        <select name="expected_intent"><option value="">(same)</option>${intents.map((i) => `<option ${i === l.expected_intent ? "selected" : ""}>${i}</option>`).join("")}</select></label>
    </div>
    <label class="field" id="expected-field" ${l.verdict === "bad" ? "" : "hidden"}>Expected behavior (becomes the eval assertion)
      <textarea name="expected_behavior" maxlength="2000" placeholder="e.g. suggest earbuds that fit the new budget">${esc(l.expected_behavior || "")}</textarea></label>
    <label class="field">Note
      <textarea name="note" maxlength="2000" placeholder="What went wrong / why it's good">${esc(l.note || "")}</textarea></label>
    <div class="row-actions">
      <button class="btn" type="submit" ${l.verdict ? "" : "disabled"}>Save label</button>
      ${l.verdict ? `<button class="btn ghost" type="button" id="del-label">Remove</button>` : ""}
      <span class="saved" id="label-saved">${l.updated_at ? `saved ${ago(l.updated_at)} · prompt ${esc(l.prompt_version)}` : ""}</span>
    </div>
  </form>`;
}

function wireLabelForm(d) {
  const f = $("#label-form");
  let verdict = d.label?.verdict || null;
  $$("[data-v]", f).forEach((b) => b.addEventListener("click", () => {
    verdict = b.dataset.v;
    $$("[data-v]", f).forEach((x) => x.setAttribute("aria-pressed", String(x === b)));
    $("#bad-fields").hidden = $("#expected-field").hidden = verdict !== "bad";
    $("button[type=submit]", f).disabled = false;
  }));
  f.addEventListener("submit", async (e) => {
    e.preventDefault();
    const fd = new FormData(f);
    try {
      const saved = await api("/api/labels", { method: "POST", body: {
        session_id: d.session_id, turn: d.turn, verdict,
        category: verdict === "bad" ? fd.get("category") : null,
        expected_intent: verdict === "bad" ? (fd.get("expected_intent") || null) : null,
        expected_behavior: verdict === "bad" ? fd.get("expected_behavior") : "",
        note: fd.get("note") || "",
      } });
      d.label = saved;
      renderDetail(d);
      loadTraces(); loadStats();
    } catch (err) { $("#label-saved").textContent = err.message; }
  });
  $("#del-label")?.addEventListener("click", async () => {
    await api(`/api/labels/${encodeURIComponent(d.session_id)}/${d.turn}`, { method: "DELETE" });
    d.label = null;
    renderDetail(d);
    loadTraces(); loadStats();
  });
}

// ---------------------------------------------------------------- breakers & chaos
const PRESETS = [
  ["Down", { down: true }],
  ["30% errors", { error_rate: 0.3 }],
  ["30% timeouts", { timeout_rate: 0.3 }],
  ["Malformed", { malformed_rate: 1 }],
  ["+800ms", { latency_ms: 800 }],
];

function faultText(f) {
  const parts = [];
  if (f.down) parts.push("down");
  if (f.error_rate) parts.push(`${pct(f.error_rate)} errors`);
  if (f.timeout_rate) parts.push(`${pct(f.timeout_rate)} timeouts`);
  if (f.malformed_rate) parts.push(`${pct(f.malformed_rate)} malformed`);
  if (f.latency_ms) parts.push(`+${f.latency_ms}ms`);
  return `${parts.join(", ") || "no-op"} · injected ${f.injected}`;
}

async function loadHealthView() {
  const h = ui.health || (await loadHealth());
  const byName = Object.fromEntries((h.breakers || []).map((b) => [b.name, b]));
  const disabled = h.chaos_enabled ? "" : "disabled";
  $("#deps").innerHTML = h.dependencies.map((name) => {
    const b = byName[name];
    const st = b ? b.state : "idle";
    const f = h.faults[name];
    const matches = (pre) => f && Object.entries(pre).every(([k, v]) => f[k] === v);
    return `<div class="dep${f ? " faulted" : ""}">
      <div class="dep-head"><span class="dep-name mono">${esc(name)}</span><span class="state ${st}">${st === "idle" ? "not called" : st.replace("_", "-")}</span></div>
      <div class="meter" title="window error rate"><div style="width:${b ? b.window_error_rate * 100 : 0}%"></div></div>
      <div class="dep-nums"><span>err ${b ? pct(b.window_error_rate) : "—"} of ${b ? b.window_size : 0}</span><span>calls ${b?.calls ?? 0}</span><span>failures ${b?.failures ?? 0}</span><span>rejected ${b?.rejected ?? 0}</span></div>
      ${f ? `<div class="fault-line">Fault: ${esc(faultText(f))}</div>` : ""}
      <div class="dep-sub">Breaker</div>
      <div class="row-actions">
        <button class="btn ghost sm" data-br="${esc(name)}" data-st="open" ${disabled}>Force open</button>
        <button class="btn ghost sm" data-br="${esc(name)}" data-st="closed" ${disabled}>Force closed</button>
      </div>
      <div class="dep-sub">Inject fault</div>
      <div class="row-actions">
        ${PRESETS.map(([lbl, pre], i) => `<button class="btn ghost sm${matches(pre) ? " on" : ""}" data-fault="${esc(name)}" data-p="${i}" ${disabled}>${lbl}</button>`).join("")}
        ${f ? `<button class="btn danger-ghost sm" data-clear="${esc(name)}" ${disabled}>Clear</button>` : ""}
      </div>
    </div>`;
  }).join("");
  $$("[data-br]").forEach((b) => b.addEventListener("click", () =>
    act(() => api(`/api/admin/breakers/${encodeURIComponent(b.dataset.br)}/${b.dataset.st}`, { method: "POST" }))));
  $$("[data-fault]").forEach((b) => b.addEventListener("click", () =>
    act(() => api(`/api/admin/faults/${encodeURIComponent(b.dataset.fault)}`, { method: "POST", body: PRESETS[Number(b.dataset.p)][1] }))));
  $$("[data-clear]").forEach((b) => b.addEventListener("click", () =>
    act(() => api(`/api/admin/faults/${encodeURIComponent(b.dataset.clear)}`, { method: "DELETE" }))));
  $("#clear-faults").disabled = !h.chaos_enabled;

  const ev = [...(h.breaker_events || [])].reverse();
  $("#events").innerHTML = ev.length ? ev.map((e) =>
    `<div class="event"><span class="t">${clock(e.ts)}</span><span><b class="mono">${esc(e.name)}</b> ${esc(e.from_state)} → <span class="state ${esc(e.to_state)}">${esc(e.to_state.replace("_", "-"))}</span> <span style="color:var(--muted)">${esc(e.reason)}</span></span></div>`).join("")
    : `<div class="empty">No breaker transitions yet.</div>`;
}

async function act(fn) {
  try { await fn(); } catch (e) { banner(e.message); return; }
  await loadHealth();
  loadHealthView();
}
$("#clear-faults").addEventListener("click", () => act(() => api("/api/admin/faults", { method: "DELETE" })));

// ---------------------------------------------------------------- tickets
async function loadTickets() {
  const rows = await api("/api/tickets?limit=200");
  $("#tickets").innerHTML = rows.length ? `<thead><tr><th>Ticket</th><th>Issue</th><th>Priority</th><th>Order</th><th>Status</th><th>Created</th><th>Summary</th></tr></thead><tbody>${
    rows.map((t) => `<tr><td class="mono">${esc(t.ticket_id)}</td><td>${esc(t.issue_type)}</td>
      <td><span class="pill ${t.priority === "high" ? "bad" : ""}">${esc(t.priority)}</span></td>
      <td class="mono">${esc(t.order_id || "—")}</td><td>${esc(t.status)}</td><td>${ago(t.created_at)}</td>
      <td class="wrap">${esc(t.summary)} <button class="btn ghost sm" data-sess="${esc(t.session_id)}">Traces</button></td></tr>`).join("")}</tbody>`
    : `<tbody><tr><td class="empty">No tickets yet.</td></tr></tbody>`;
  $$("[data-sess]").forEach((b) => b.addEventListener("click", () => { setTab("traces"); setSession(b.dataset.sess); }));
}

// ---------------------------------------------------------------- labels
async function loadLabels() {
  const rows = await api("/api/labels");
  $("#labels").innerHTML = rows.length ? rows.map((l) => `<div class="lbl">
      <div class="lbl-top"><span>${labelPill(l)} <span class="mono">${esc(l.session_id)} · turn ${l.turn}</span></span>
        <span class="row-actions"><span style="color:var(--muted);font-size:12px">${ago(l.updated_at)} · prompt ${esc(l.prompt_version)}</span>
        ${l.trace_id ? `<button class="btn ghost sm" data-open="${esc(l.trace_id)}">Open trace</button>` : ""}</span></div>
      ${l.expected_intent ? `<div class="lbl-note"><b>Expected intent:</b> ${esc(l.expected_intent)}</div>` : ""}
      ${l.expected_behavior ? `<div class="lbl-note"><b>Expected:</b> ${esc(l.expected_behavior)}</div>` : ""}
      ${l.note ? `<div class="lbl-note"><b>Note:</b> ${esc(l.note)}</div>` : ""}
    </div>`).join("")
    : `<div class="empty">No labels yet. Open a trace and mark it good or bad.</div>`;
  $$("[data-open]").forEach((b) => b.addEventListener("click", () => { setTab("traces"); openTrace(b.dataset.open); }));
}

async function exportLabels(verdict) {
  try {
    const r = await api(`/api/labels/export?verdict=${verdict}`, { raw: true });
    const blob = await r.blob();
    const a = Object.assign(document.createElement("a"), { href: URL.createObjectURL(blob), download: `labelled_cases_${verdict}.jsonl` });
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  } catch (e) { banner(e.message); }
}
$("#export-bad").addEventListener("click", () => exportLabels("bad"));
$("#export-all").addEventListener("click", () => exportLabels("all"));

// ---------------------------------------------------------------- refresh loop
let busy = false;
async function refresh() {
  if (busy) return;
  busy = true;
  try {
    await loadHealth();
    if (ui.tab === "traces") {
      await Promise.all([loadStats(), loadTraces()]);
    } else if (ui.tab === "health") await loadHealthView();
    else if (ui.tab === "tickets") await loadTickets();
    else if (ui.tab === "labels") await loadLabels();
  } catch (e) {
    if (e.message !== "Admin token required") banner(`Can't reach the API: ${e.message}`);
  } finally { busy = false; }
}

setInterval(() => {
  const typing = document.activeElement && ["TEXTAREA", "INPUT", "SELECT"].includes(document.activeElement.tagName);
  if ($("#live").checked && !document.hidden && !typing && !$("#token-dialog").open) refresh();
}, POLL_MS);

document.addEventListener("keydown", (e) => { if (e.key === "Escape" && ui.selected && !$("#token-dialog").open) closeDetail(); });

const initial = (location.hash || "").slice(1);
setTab(["traces", "health", "tickets", "labels"].includes(initial) ? initial : "traces");
