// Atlas PWA client.
// Client-side harness pieces: per-message request_id (server dedupes retries),
// bounded retry with backoff on network/5xx, 409 handling, offline detection,
// and a visible banner when the server reports a degraded answer.

const $ = (s) => document.querySelector(s);
const thread = $("#thread");
const input = $("#input");
const sendBtn = $("#send");
const banner = $("#banner");

const store = {
  get(k) { try { return localStorage.getItem(k); } catch { return null; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch { /* private mode */ } },
  del(k) { try { localStorage.removeItem(k); } catch { /* ignore */ } },
};

let sessionId = store.get("atlas.session");
let busy = false;

const DEGRADE_COPY = {
  shipping_unavailable: "Delivery and tax estimates are temporarily unavailable for some items.",
  catalog_unavailable: "Our catalog is having trouble right now.",
  rules_router: "Running in backup mode — answers may be simpler than usual.",
  template_reply: "Running in backup mode — answers may be simpler than usual.",
  deadline: "That took longer than expected.",
  retrieval_unavailable: "Policy lookup is temporarily unavailable.",
  protection_quote_unavailable: "Protection pricing is temporarily unavailable.",
  ticket_unavailable: "We couldn't reach support just now.",
};

const rid = () => (crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + Math.random()).replace(/-/g, "").slice(0, 20);
const money = (v) => "$" + Number(v).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };

function scrollDown() { requestAnimationFrame(() => { thread.scrollTop = thread.scrollHeight; }); }

function addUser(text) {
  $("#welcome")?.remove();
  const row = el("div", "row user");
  row.append(el("div", "bubble", text));
  const meta = el("div", "meta", "Sending…");
  row.append(meta);
  thread.append(row);
  scrollDown();
  return meta;
}

function addTyping() {
  const row = el("div", "row bot");
  const b = el("div", "bubble typing");
  b.innerHTML = "<span></span><span></span><span></span>";
  b.setAttribute("aria-label", "Assistant is typing");
  row.append(b);
  thread.append(row);
  scrollDown();
  return row;
}

const ICONS = {
  electronics: '<path d="M4 14v-2a8 8 0 0116 0v2M4 14h3v5H4zM17 14h3v5h-3z" />',
  apparel: '<path d="M8 4l-4 3 2 3 2-1v11h8V9l2 1 2-3-4-3c-1 2-2.5 3-4 3S9 6 8 4z" />',
  home: '<path d="M4 11l8-6 8 6v9H4z M10 20v-5h4v5" />',
  beauty: '<path d="M9 3h6v4H9zM8 7h8v14H8z" />',
  outdoor: '<path d="M3 20l7-12 4 6 2-3 5 9z" />',
};

function card(item) {
  const c = el("article", "card");
  const thumb = el("div", "card-thumb");
  thumb.innerHTML = `<svg viewBox="0 0 24 24" width="36" height="36" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round" aria-hidden="true">${ICONS[item.category] || ICONS.home}</svg>`;
  c.append(thumb);
  c.append(el("div", "card-title", item.title));
  c.append(el("div", "card-merchant", item.merchant));
  const pr = el("div", "card-price");
  pr.append(el("span", "price", money(item.price_usd)));
  pr.append(el("span", "rating", `★ ${item.rating} · ${item.reviews.toLocaleString()} reviews`));
  c.append(pr);

  const badges = el("div", "badges");
  if (item.final_sale) badges.append(el("span", "badge warn", "Final sale"));
  const sh = item.shipping;
  if (sh) {
    if (sh.delay_risk === "high") badges.append(el("span", "badge warn", "Peak-season delays likely"));
    if (sh.collected === "at checkout") badges.append(el("span", "badge ok", "Taxes paid at checkout"));
    else badges.append(el("span", "badge warn", "Taxes due on delivery"));
  }
  if (badges.childElementCount) c.append(badges);

  const dl = el("dl", "facts");
  const row = (k, v, cls) => { dl.append(el("dt", null, k)); dl.append(el("dd", cls || null, v)); };
  if (sh) {
    row("Delivery", `${sh.days[0]}–${sh.days[1]} days`);
    row("Shipping", sh.fee_usd ? money(sh.fee_usd) : "Free");
    row(`${sh.tax_name} & duties`, money(sh.taxes_usd));
    row("Est. total", money(sh.landed_total_usd), "total");
  } else {
    row("Delivery", "Tell me the destination");
  }
  c.append(dl);
  return c;
}

function quoteCard(q) {
  const c = el("article", "panel");
  c.append(el("div", "panel-kicker", "Protection options"));
  c.append(el("div", "card-title", q.title));
  c.append(el("div", "card-merchant", `Item ${money(q.item_price_usd)} · shipping to ${q.country}`));
  q.options.forEach((o) => {
    const opt = el("div", "opt" + (o.recommended ? " rec" : ""));
    const head = el("div", "opt-head");
    head.append(el("span", "opt-name", o.name));
    head.append(el("span", "opt-price", money(o.price_usd)));
    opt.append(head);
    opt.append(el("div", "opt-covers", o.covers));
    const verdict = el("div", "opt-verdict", o.recommended ? "Worth considering" : "Probably not needed");
    opt.append(verdict);
    if (o.recommended && o.reasons.length) {
      const ul = el("ul", "opt-reasons");
      o.reasons.forEach((r) => ul.append(el("li", null, r)));
      opt.append(ul);
    }
    c.append(opt);
  });
  if (q.low_value_note) c.append(el("div", "panel-note", q.low_value_note));
  c.append(el("div", "panel-note", "Add at checkout only. Claims are reviewed; approval isn't guaranteed."));
  return c;
}

function ticketCard(t) {
  const c = el("article", "panel ticket");
  c.append(el("div", "panel-kicker", t.deduplicated ? "Existing support ticket" : "Support ticket opened"));
  const head = el("div", "opt-head");
  head.append(el("span", "opt-name", t.ticket_id));
  head.append(el("span", "badge " + (t.priority === "high" ? "warn" : "ok"), t.priority === "high" ? "High priority" : "Normal priority"));
  c.append(head);
  const dl = el("dl", "facts");
  const row = (k, v) => { dl.append(el("dt", null, k)); dl.append(el("dd", null, v)); };
  row("Issue", (t.issue_type || "other").replace("_", " "));
  row("Order", t.order_id || "Not provided");
  row("Status", "Open — a specialist will follow up");
  c.append(dl);
  return c;
}

function addBot(resp) {
  const row = el("div", "row bot");
  for (const m of resp.messages) {
    if (m.type === "text") row.append(el("div", "bubble", m.text));
    if (m.type === "product_cards") {
      const wrap = el("div", "cards");
      m.items.forEach((it) => wrap.append(card(it)));
      row.append(wrap);
    }
    if (m.type === "protection_quote") row.append(quoteCard(m.quote));
    if (m.type === "ticket") row.append(ticketCard(m.ticket));
    if (m.type === "sources" && m.items.length) {
      const det = el("details", "sources");
      det.append(el("summary", null, `Sources (${m.items.length})`));
      const ol = el("ol");
      m.items.forEach((x) => {
        const li = el("li");
        li.value = x.n;
        li.append(el("div", "src-title", `${x.title} — ${x.section}`));
        li.append(el("div", "src-snippet", x.snippet));
        ol.append(li);
      });
      det.append(ol);
      row.append(det);
    }
    if (m.type === "chips") {
      const wrap = el("div", "chips");
      m.options.forEach((o) => { const b = el("button", "chip", o); b.type = "button"; b.onclick = () => send(o); wrap.append(b); });
      row.append(wrap);
    }
  }
  thread.append(row);
  const notes = [...new Set((resp.degraded || []).map((d) => DEGRADE_COPY[d]).filter(Boolean))];
  showBanner(notes.join(" "));
  scrollDown();
}

function showBanner(text) {
  banner.hidden = !text;
  banner.textContent = text || "";
}

async function postChat(payload, meta) {
  const delays = [600, 1500];
  for (let attempt = 0; ; attempt++) {
    try {
      const r = await fetch("/api/chat", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
      if (r.ok) return await r.json();
      if (r.status === 409 && attempt < delays.length) { meta.textContent = "Waiting for the previous reply…"; }
      else if (r.status >= 500 && attempt < delays.length) { meta.textContent = "Retrying…"; }
      else throw new Error(`HTTP ${r.status}`);
    } catch (e) {
      if (attempt >= delays.length || !navigator.onLine) throw e;
      meta.textContent = "Connection hiccup, retrying…";
    }
    await new Promise((res) => setTimeout(res, delays[attempt]));
  }
}

async function send(text, existing) {
  text = text.trim();
  if (!text || busy) return;
  if (!navigator.onLine) { showBanner("You're offline. Messages will send when you're back online."); return; }
  busy = true; sendBtn.disabled = true;
  const meta = existing?.meta || addUser(text);
  const payload = existing?.payload || { message: text, session_id: sessionId, request_id: rid() };
  const typing = addTyping();
  try {
    const resp = await postChat(payload, meta);
    sessionId = resp.session_id; store.set("atlas.session", sessionId);
    meta.textContent = resp.replayed ? "Delivered (recovered)" : "Delivered";
    typing.remove();
    addBot(resp);
  } catch (e) {
    typing.remove();
    meta.className = "meta err";
    meta.textContent = "Not sent. ";
    const retry = el("button", null, "Tap to retry");
    retry.type = "button";
    // Same request_id: if the server already processed it, we get the stored reply, not a duplicate turn.
    retry.onclick = () => { meta.className = "meta"; meta.textContent = "Sending…"; send(text, { meta, payload }); };
    meta.append(retry);
  } finally {
    busy = false; sendBtn.disabled = false; input.focus();
  }
}

$("#composer").addEventListener("submit", (e) => { e.preventDefault(); const t = input.value; input.value = ""; autosize(); send(t); });
input.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); $("#composer").requestSubmit(); } });
function autosize() { input.style.height = "auto"; input.style.height = Math.min(input.scrollHeight, 120) + "px"; }
input.addEventListener("input", autosize);
document.querySelectorAll("#starter-chips .chip").forEach((b) => b.addEventListener("click", () => send(b.textContent)));
$("#new-chat").addEventListener("click", () => { store.del("atlas.session"); location.reload(); });
window.addEventListener("offline", () => showBanner("You're offline."));
window.addEventListener("online", () => showBanner(""));

async function restore() {
  if (!sessionId) return;
  try {
    const r = await fetch(`/api/sessions/${sessionId}`);
    if (!r.ok) { store.del("atlas.session"); sessionId = null; return; }
    const data = await r.json();
    for (const t of data.turns) {
      const meta = addUser(t.user_message); meta.textContent = "Delivered";
      addBot(t.response);
    }
    showBanner("");
  } catch { /* offline: keep empty thread */ }
}

if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(() => {});
restore();
