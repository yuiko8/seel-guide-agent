// App E2E: core scenarios in the shopper PWA at iPhone 13 size, plus a console smoke test.
// Starts its own server (mock LLM, temp DB) so it is self-contained:  node e2e/app.e2e.js
// Env: E2E_BASE_URL to test an already-running server instead; E2E_SHOTS=dir to save screenshots.
const { chromium, devices } = require("playwright");
const { spawn } = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");

const ROOT = path.resolve(__dirname, "..");
const PORT = 8000 + Math.floor(Math.random() * 900) + 100;
let BASE = process.env.E2E_BASE_URL || `http://127.0.0.1:${PORT}`;
const SHOTS = process.env.E2E_SHOTS;
if (SHOTS) fs.mkdirSync(SHOTS, { recursive: true });

async function startServer() {
  if (process.env.E2E_BASE_URL) return null;
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "e2e-"));
  const py = fs.existsSync(path.join(ROOT, ".venv/bin/python")) ? path.join(ROOT, ".venv/bin/python") : "python";
  const proc = spawn(py, ["-m", "uvicorn", "app.api:create_app", "--factory", "--port", String(PORT)], {
    cwd: ROOT, stdio: ["ignore", "ignore", "pipe"],
    env: { ...process.env, LLM_MODE: "mock", SIM_TODAY: "2026-11-21", DB_PATH: path.join(tmp, "e2e.db"),
      TRACE_JSONL: path.join(tmp, "t.jsonl"), ADMIN_CHAOS: "1", ADMIN_TOKEN: "" },
  });
  let err = "";
  proc.stderr.on("data", (d) => (err += d));
  for (let i = 0; i < 100; i++) {
    try { if ((await fetch(`${BASE}/api/health`)).ok) return proc; } catch { /* not up yet */ }
    await new Promise((r) => setTimeout(r, 150));
  }
  proc.kill();
  throw new Error("server did not start:\n" + err);
}

const results = [];
async function scenario(name, fn) {
  const t0 = Date.now();
  try { await fn(); results.push({ name, ok: true, ms: Date.now() - t0 }); }
  catch (e) { results.push({ name, ok: false, ms: Date.now() - t0, error: String(e.message || e).split("\n")[0] }); }
}
function assert(cond, msg) { if (!cond) throw new Error(msg); }

async function newPage(browser) {
  const ctx = await browser.newContext({ ...devices["iPhone 13"], serviceWorkers: "block" });
  const page = await ctx.newPage();
  page.errors = [];
  page.on("pageerror", (e) => page.errors.push(e.message));
  await page.goto(BASE + "/");
  return page;
}
async function send(page, text) {
  const before = await page.locator(".row.bot").count();
  await page.fill("#input", text);
  await page.click("#send");
  await page.waitForFunction((n) => document.querySelectorAll(".row.bot:not(:has(.typing))").length > n, before, { timeout: 10000 });
  return page.locator(".row.bot").last();
}
async function noOverflow(page, where) {
  const [sw, iw] = await page.evaluate(() => [document.documentElement.scrollWidth, window.innerWidth]);
  assert(sw <= iw, `${where}: horizontal overflow ${sw} > ${iw}`);
}
async function shot(page, name) { if (SHOTS) await page.screenshot({ path: path.join(SHOTS, `${name}.png`) }); }

(async () => {
  const server = await startServer();
  const browser = await chromium.launch();
  try {
    await scenario("S1 starter chip -> product cards within budget, with delivery + taxes", async () => {
      const p = await newPage(browser);
      await p.click("#starter-chips .chip >> nth=0");
      await p.waitForSelector(".card");
      const prices = await p.$$eval(".card .price", (els) => els.map((e) => parseFloat(e.textContent.replace(/[^0-9.]/g, ""))));
      assert(prices.length >= 2 && prices.every((x) => x <= 150), `prices ${prices}`);
      assert(await p.locator(".card .facts").count() === prices.length, "each card shows delivery/tax facts");
      await noOverflow(p, "S1"); await shot(p, "s1");
      assert(!p.errors.length, p.errors.join("; "));
    });
    await scenario("S4 protection nudge chip -> quote panel with honest verdicts", async () => {
      const p = await newPage(browser);
      await send(p, "headphones under $300 to Japan");
      await p.click(".chip:has-text('protection')");
      await p.waitForSelector(".panel:has-text('Protection options')");
      assert(await p.locator(".opt").count() === 2, "two protection options");
      assert(await p.locator(".panel:has-text(\"approval isn't guaranteed\")").count() === 1, "boundary note shown");
      await noOverflow(p, "S4"); await shot(p, "s4");
    });
    await scenario("S3 policy question -> cited answer with sources", async () => {
      const p = await newPage(browser);
      const bot = await send(p, "What's the return policy at Nordlys?");
      assert(/\[1\]/.test(await bot.locator(".bubble").innerText()), "reply cites [1]");
      await bot.locator(".sources summary").click();
      assert(await bot.locator(".sources li").count() >= 1, "sources listed");
      await noOverflow(p, "S3"); await shot(p, "s3");
    });
    await scenario("S5 restricted item -> no cards, reason given", async () => {
      const p = await newPage(browser);
      const bot = await send(p, "power bank to Australia");
      assert(await p.locator(".card").count() === 0, "no product cards");
      assert(/lithium/i.test(await bot.innerText()), "explains the lithium restriction");
    });
    await scenario("S6 lost parcel -> ticket card, no refund promise", async () => {
      const p = await newPage(browser);
      const bot = await send(p, "My parcel never arrived, order #AB12345");
      await p.waitForSelector(".panel.ticket");
      const txt = (await bot.innerText()).toLowerCase();
      assert(!/you('ll| will) (get|receive) a (full )?refund/.test(txt), "no refund promise");
      await noOverflow(p, "S6"); await shot(p, "s6");
    });
    await scenario("Session survives reload", async () => {
      const p = await newPage(browser);
      await send(p, "headphones under $150 to Germany");
      await p.reload();
      await p.waitForSelector(".card", { timeout: 5000 });
      assert(await p.locator(".row.user").count() >= 1, "user message restored");
    });
    await scenario("Degraded answer shows a banner (shipping outage)", async () => {
      await fetch(`${BASE}/api/admin/faults/tool:shipping`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ down: true }) });
      try {
        const p = await newPage(browser);
        await send(p, "headphones under $200 to Japan");
        await p.waitForSelector("#banner:not([hidden])");
        assert(await p.locator(".card").count() >= 1, "still recommends");
        await shot(p, "degraded");
      } finally {
        await fetch(`${BASE}/api/admin/faults`, { method: "DELETE" });
        await fetch(`${BASE}/api/admin/breakers/tool:shipping/closed`, { method: "POST" });
      }
    });
    await scenario("Ops console lists the turns and opens a trace", async () => {
      const p = await newPage(browser);
      await p.goto(BASE + "/console/");
      await p.waitForSelector(".item");
      await p.locator(".item").first().click();
      await p.waitForSelector("#wf .wf-row");
      await noOverflow(p, "console"); await shot(p, "console");
      assert(!p.errors.length, p.errors.join("; "));
    });
  } finally {
    await browser.close();
    if (server) server.kill();
  }
  for (const r of results) console.log(`${r.ok ? "PASS" : "FAIL"}  ${r.name} (${r.ms}ms)${r.ok ? "" : "\n      " + r.error}`);
  const failed = results.filter((r) => !r.ok).length;
  console.log(`\n${results.length - failed}/${results.length} E2E scenarios passed`);
  process.exit(failed ? 1 : 0);
})().catch((e) => { console.error(e); process.exit(1); });
