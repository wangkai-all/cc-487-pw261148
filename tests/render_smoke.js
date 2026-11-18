/* Headless render smoke test: loads the real static bundle in jsdom against a
   live server and switches through every tab, failing on any page error. */
const fs = require("node:fs");
const path = require("node:path");
let JSDOM, VirtualConsole;
try {
  ({ JSDOM, VirtualConsole } = require("jsdom"));
} catch {
  ({ JSDOM, VirtualConsole } = require("/tmp/jsdomtest/node_modules/jsdom"));
}

const BASE = process.env.BASE || "http://127.0.0.1:8787";
const ROOT = path.resolve(__dirname, "..");
const SERVER = process.env.SERVER || BASE;

async function main() {
  const errors = [];
  const virtualConsole = new VirtualConsole();
  virtualConsole.on("jsdomError", (err) => errors.push(String(err.message || err)));
  virtualConsole.on("error", (msg) => errors.push(String(msg)));

  try {
    const health = await fetch(SERVER + "/api/health");
    if (!health.ok) throw new Error(String(health.status));
  } catch (err) {
    console.error(`No server at ${SERVER}; start one with: python3 cli.py serve --port 8787`);
    process.exit(2);
  }

  const html = fs.readFileSync(path.join(ROOT, "static/index.html"), "utf8");
  const dom = new JSDOM(html, {
    url: BASE + "/",
    runScripts: "outside-only",
    pretendToBeVisual: true,
    virtualConsole,
  });
  const { window } = dom;
  window.fetch = (url, opts) => fetch(new URL(url, BASE), opts);
  window.alert = (msg) => errors.push(`alert: ${msg}`);
  window.matchMedia = window.matchMedia || (() => ({ matches: false, addEventListener() {} }));
  window.HTMLDialogElement.prototype.showModal = function () { this.open = true; };
  window.HTMLDialogElement.prototype.close = function () { this.open = false; };

  window.eval(fs.readFileSync(path.join(ROOT, "static/app.js"), "utf8"));

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  for (let i = 0; i < 100 && !window.document.querySelector("#exp-title").textContent.includes("Checkout") && !errors.length; i++) {
    await sleep(50);
  }
  await sleep(300);

  const tabs = ["overview", "balance", "attrition", "metrics", "versions", "checks"];
  const report = {};
  for (const tab of tabs) {
    window.document.querySelector(`.tabs button[data-tab="${tab}"]`).click();
    await sleep(500);
    const view = window.document.getElementById("view");
    report[tab] = {
      cards: view.querySelectorAll(".card").length,
      svgs: view.querySelectorAll("svg").length,
      tables: view.querySelectorAll("table").length,
      textLength: view.textContent.trim().length,
      hasError: /TypeError|undefined is not|NaN/.test(view.textContent),
    };
    if (tab === "metrics") {
      // switch populations
      for (const btn of [...view.querySelectorAll("[data-pop]")]) {
        btn.click();
        await sleep(200);
        const v = window.document.getElementById("view");
        report[`metrics:${btn.dataset.pop}`] = {
          rows: v.querySelectorAll("tbody tr").length,
          svgs: v.querySelectorAll("svg").length,
        };
      }
    }
    if (tab === "versions") {
      await sleep(600);
      report["versions:compare"] = {
        rendered: window.document.getElementById("cmp-out")?.textContent.trim().length || 0,
      };
      // Verify the newest version through the headline button, then every row.
      window.document.querySelector("#verify-current").click();
      await sleep(600);
      report["versions:verify"] = {
        rendered: window.document.getElementById("verify-out")?.textContent.trim().length || 0,
        verdict: window.document.getElementById("verify-out")?.textContent.match(/Snapshot verified|Drift detected|still active/)?.[0] || null,
      };
      // A frozen version must report a real verdict, not the active notice.
      const rowBtns = [...window.document.querySelectorAll("[data-verify]")];
      const verdicts = [];
      for (const btn of rowBtns) {
        btn.click();
        await sleep(600);
        const txt = window.document.getElementById("verify-out")?.textContent || "";
        verdicts.push(txt.match(/Snapshot verified|Drift detected|still active/)?.[0] || null);
      }
      report["versions:verify:rows"] = verdicts;
      if (rowBtns.length > 1 && !verdicts.some((v) => v === "Snapshot verified" || v === "Drift detected")) {
        errors.push("no frozen version produced a snapshot verdict");
      }
    }
    if (tab === "checks") {
      const html = view.innerHTML;
      report["checks:unrendered"] = html.includes("[object Object]") || html.includes("NaN");
    }
  }

  console.log(JSON.stringify({ report, errors }, null, 2));
  if (errors.length) process.exitCode = 1;
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
