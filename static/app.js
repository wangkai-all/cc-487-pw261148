/* global fetch, document, window */
"use strict";

const state = {
  experiments: [],
  expId: null,
  detail: null,
  versionId: null,
  tab: "overview",
  population: "intention_to_treat",
  live: false,
};

const POPULATIONS = [
  ["intention_to_treat", "ITT"],
  ["as_treated", "As-treated"],
  ["per_protocol", "Per protocol"],
  ["assigned", "Raw assigned"],
];

// ---------------------------------------------------------------- utilities
const fmt = {
  n(v) {
    if (v === null || v === undefined) return "—";
    return Math.round(v).toLocaleString("en-US");
  },
  pct(v, digits = 1) {
    if (v === null || v === undefined || Number.isNaN(v)) return "—";
    return `${(v * 100).toFixed(digits)}%`;
  },
  pp(v, digits = 2) {
    if (v === null || v === undefined) return "—";
    const x = v * 100;
    return `${x >= 0 ? "+" : ""}${x.toFixed(digits)} pp`;
  },
  num(v, digits = 3) {
    if (v === null || v === undefined || Number.isNaN(v)) return "—";
    if (Math.abs(v) >= 1e5) return v.toExponential(2);
    return v.toFixed(digits);
  },
  pval(v) {
    if (v === null || v === undefined) return "—";
    if (v < 0.001) return "< 0.001";
    return v.toFixed(3);
  },
  signed(v, digits = 2) {
    if (v === null || v === undefined) return "—";
    return `${v >= 0 ? "+" : ""}${v.toFixed(digits)}`;
  },
  time(iso) {
    if (!iso) return "—";
    return iso.replace("T", " ").replace("Z", " UTC").replace(/\.\d+/, "");
  },
  dur(seconds) {
    if (seconds === null || seconds === undefined) return "—";
    const abs = Math.abs(seconds);
    if (abs < 90) return `${Math.round(seconds)}s`;
    if (abs < 5400) return `${(seconds / 60).toFixed(1)}m`;
    if (abs < 172800) return `${(seconds / 3600).toFixed(1)}h`;
    return `${(seconds / 86400).toFixed(1)}d`;
  },
};

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function el(html) {
  const tpl = document.createElement("template");
  tpl.innerHTML = html.trim();
  return tpl.content.firstElementChild;
}

function badge(status, text) {
  const label = text ?? status;
  const icon = { pass: "✓", warn: "!", fail: "✕", info: "i" }[status] || "";
  return `<span class="badge ${esc(status)}">${icon ? `<span aria-hidden="true">${icon}</span>` : '<span class="dot"></span>'}${esc(label)}</span>`;
}

function statusBadge(status) {
  const text = { active: "Active", paused: "Paused", rolled_back: "Rolled back", draft: "Draft" }[status] || status;
  return `<span class="badge ${esc(status)}">${esc(text)}</span>`;
}

function effectCell(m) {
  const e = m.effect || {};
  if (e.diff === null || e.diff === undefined) return `<span class="muted">no data</span>`;
  const isProp = m.type === "proportion";
  const val = isProp ? fmt.pp(e.diff) : fmt.signed(e.diff, 3);
  const ci = isProp
    ? `[${fmt.pp(e.ci_low)}, ${fmt.pp(e.ci_high)}]`
    : `[${fmt.signed(e.ci_low, 3)}, ${fmt.signed(e.ci_high, 3)}]`;
  const sig = e.significant ? `<strong>${val}</strong>` : val;
  return `<div>${sig}</div><div class="small muted mono-num">95% CI ${ci} · p=${fmt.pval(e.p_value)}</div>`;
}

// ---------------------------------------------------------------- API
async function api(path, options) {
  const res = await fetch(path, options);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `${res.status} ${res.statusText}`);
  return data;
}

async function post(path, body) {
  return api(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
}

// ---------------------------------------------------------------- tooltip
const tip = () => document.getElementById("tooltip");
function showTip(evt, text) {
  const node = tip();
  node.textContent = text;
  node.style.left = `${evt.clientX}px`;
  node.style.top = `${evt.clientY}px`;
  node.classList.add("show");
}
function hideTip() {
  tip().classList.remove("show");
}
function bindTips(root) {
  root.querySelectorAll("[data-tip]").forEach((node) => {
    node.addEventListener("mousemove", (e) => showTip(e, node.dataset.tip));
    node.addEventListener("mouseleave", hideTip);
  });
}

// ---------------------------------------------------------------- charts
const CHART = { height: 180, padL: 44, padR: 16, padT: 12, padB: 28 };

/** Horizontal bars for one series, using 4px rounded data-end. */
function hbarChart(rows, { width = 560, barHeight = 22, gap = 12, valueFmt = fmt.n, labelWidth = 150 } = {}) {
  const chartW = width;
  const innerW = chartW - labelWidth - 56;
  const max = Math.max(1, ...rows.map((r) => r.value));
  const height = rows.length * (barHeight + gap) + gap;
  const bars = rows
    .map((r, i) => {
      const y = gap + i * (barHeight + gap);
      const w = Math.max(2, (r.value / max) * innerW);
      const colourClass = r.className || "bar-neutral";
      return `
        <text x="${labelWidth - 10}" y="${y + barHeight / 2 + 4}" text-anchor="end" class="label-secondary">${esc(r.label)}</text>
        <rect class="bar-track" x="${labelWidth}" y="${y}" width="${innerW}" height="${barHeight}" rx="4"></rect>
        <rect class="bar ${colourClass}" x="${labelWidth}" y="${y}" width="${w}" height="${barHeight}" rx="4"
              data-tip="${esc(r.tip || `${r.label}: ${valueFmt(r.value)}`)}"></rect>
        <text x="${labelWidth + w + 8}" y="${y + barHeight / 2 + 4}" class="label-strong">${esc(valueFmt(r.value))}</text>`;
    })
    .join("");
  return `<svg class="chart" viewBox="0 0 ${chartW} ${height}" role="img" aria-label="${esc(rows.map((r) => `${r.label} ${r.value}`).join(", "))}">${bars}</svg>`;
}

/** Vertical columns for control vs treatment, grouped by stratum. */
function groupedBars(groups, { width = 640, height = 220 } = {}) {
  const padL = 44, padR = 12, padT = 16, padB = 48;
  const innerW = width - padL - padR;
  const innerH = height - padT - padB;
  const max = Math.max(1, ...groups.flatMap((g) => [g.control, g.treatment]));
  const band = innerW / Math.max(1, groups.length);
  const barW = Math.min(24, Math.max(6, band / 3));
  const y = (v) => padT + innerH - (v / max) * innerH;
  const ticks = niceTicks(max, 4);
  const grid = ticks
    .map((t) => `<line class="gridline" x1="${padL}" x2="${width - padR}" y1="${y(t)}" y2="${y(t)}"></line>
      <text x="${padL - 8}" y="${y(t) + 4}" text-anchor="end">${fmt.n(t)}</text>`)
    .join("");
  const bars = groups
    .map((g, i) => {
      const cx = padL + band * i + band / 2;
      const gap = 2;
      const cH = Math.max(1, padT + innerH - y(g.control));
      const tH = Math.max(1, padT + innerH - y(g.treatment));
      return `
        <rect class="bar bar-control" x="${cx - barW - gap / 2}" y="${y(g.control)}" width="${barW}" height="${cH}" rx="4"
          data-tip="${esc(`${g.label}\ncontrol: ${fmt.n(g.control)}`)}"></rect>
        <rect class="bar bar-treatment" x="${cx + gap / 2}" y="${y(g.treatment)}" width="${barW}" height="${tH}" rx="4"
          data-tip="${esc(`${g.label}\ntreatment: ${fmt.n(g.treatment)}`)}"></rect>
        <text x="${cx}" y="${height - padB + 18}" text-anchor="middle" class="label-secondary">${esc(g.short || g.label)}</text>
        <text x="${cx}" y="${height - padB + 34}" text-anchor="middle">${fmt.pct(g.control / Math.max(1, g.control + g.treatment), 0)} / ${fmt.pct(g.treatment / Math.max(1, g.control + g.treatment), 0)}</text>`;
    })
    .join("");
  return `<svg class="chart" viewBox="0 0 ${width} ${height}" role="img" aria-label="Arm sizes by stratum">
    ${grid}<line class="axis-line" x1="${padL}" x2="${width - padR}" y1="${padT + innerH}" y2="${padT + innerH}"></line>${bars}</svg>`;
}

/** Standardized mean difference dot plot, diverging around zero. */
function smdPlot(rows, { width = 640, rowHeight = 24 } = {}) {
  const labelW = 170, rightW = 60;
  const innerW = width - labelW - rightW;
  const height = Math.max(60, rows.length * rowHeight + 34);
  const maxAbs = Math.max(0.12, ...rows.map((r) => Math.abs(r.smd || 0))) * 1.15;
  const x = (v) => labelW + ((v + maxAbs) / (2 * maxAbs)) * innerW;
  const zero = x(0);
  const grid = [-0.1, 0.1].map((v) => `<line class="gridline" x1="${x(v)}" x2="${x(v)}" y1="14" y2="${height - 18}"></line>`).join("");
  const dots = rows
    .map((r, i) => {
      const cy = 24 + i * rowHeight;
      const px = x(r.smd || 0);
      const cls = Math.abs(r.smd || 0) > 0.1 ? "point-treatment" : "point-control";
      return `
        <text x="${labelW - 10}" y="${cy + 4}" text-anchor="end" class="label-secondary">${esc(r.label)}</text>
        <line x1="${zero}" x2="${px}" y1="${cy}" y2="${cy}" class="ci ${Math.abs(r.smd || 0) > 0.1 ? "ci-treatment" : "ci-control"}"></line>
        <circle cx="${px}" cy="${cy}" r="4.5" class="point ${cls}" data-tip="${esc(`${r.label}\nSMD ${fmt.num(r.smd, 2)}${r.alert ? " · imbalance flagged" : ""}`)}"></circle>
        <text x="${width - 8}" y="${cy + 4}" text-anchor="end" class="${Math.abs(r.smd || 0) > 0.1 ? "label-strong" : ""}">${fmt.num(r.smd, 2)}</text>`;
    })
    .join("");
  return `<svg class="chart" viewBox="0 0 ${width} ${height}" role="img" aria-label="Covariate balance">
    ${grid}<line class="zero-strong" x1="${zero}" x2="${zero}" y1="14" y2="${height - 18}"></line>
    ${dots}<text x="${zero}" y="${height - 4}" text-anchor="middle">0</text></svg>`;
}

/** Forest plot: one row per metric, effect + 95% interval. */
function forestPlot(rows, { width = 720, rowHeight = 42 } = {}) {
  const labelW = 150, rightW = 96;
  const innerW = width - labelW - rightW;
  const height = Math.max(80, rows.length * rowHeight + 40);
  const flat = rows.flatMap((r) => [r.diff, r.ciLow, r.ciHigh].filter((v) => v !== null && v !== undefined));
  const maxAbs = Math.max(0.01, ...flat.map((v) => Math.abs(v))) * 1.1;
  const x = (v) => labelW + ((v + maxAbs) / (2 * maxAbs)) * innerW;
  const zero = x(0);
  const items = rows
    .map((r, i) => {
      const cy = 26 + i * rowHeight;
      if (r.diff === null || r.diff === undefined) {
        return `<text x="${labelW - 10}" y="${cy + 4}" text-anchor="end" class="label-secondary">${esc(r.label)}</text>
          <text x="${labelW + 10}" y="${cy + 4}" class="muted">no events</text>`;
      }
      const sig = r.ciLow !== null && r.ciHigh !== null && !(r.ciLow <= 0 && r.ciHigh >= 0);
      const cls = sig ? (r.diff >= 0 ? "ci-treatment" : "ci-control") : "ci-control";
      const dotCls = sig ? "point-treatment" : "point-control";
      return `
        <text x="${labelW - 10}" y="${cy + 4}" text-anchor="end" class="label-secondary">${esc(r.label)}</text>
        <line class="ci ${cls}" x1="${x(r.ciLow)}" x2="${x(r.ciHigh)}" y1="${cy}" y2="${cy}"></line>
        <line class="ci ${cls}" x1="${x(r.ciLow)}" x2="${x(r.ciLow)}" y1="${cy - 5}" y2="${cy + 5}"></line>
        <line class="ci ${cls}" x1="${x(r.ciHigh)}" x2="${x(r.ciHigh)}" y1="${cy - 5}" y2="${cy + 5}"></line>
        <circle class="point ${dotCls}" cx="${x(r.diff)}" cy="${cy}" r="5"
          data-tip="${esc(`${r.label}\n${r.diffLabel}\n95% CI ${r.ciLabel}\np=${fmt.pval(r.p)}`)}"></circle>
        <text x="${width - 8}" y="${cy + 4}" text-anchor="end" class="${sig ? "label-strong" : ""}">${esc(r.diffLabel)}</text>`;
    })
    .join("");
  return `<svg class="chart" viewBox="0 0 ${width} ${height}" role="img" aria-label="Treatment effect with 95% confidence intervals">
    <line class="zero-strong" x1="${zero}" x2="${zero}" y1="14" y2="${height - 22}"></line>
    <text x="${zero}" y="${height - 6}" text-anchor="middle">0 · favours control ↑ favours treatment</text>
    ${items}</svg>`;
}

function niceTicks(max, count) {
  const raw = max / count;
  const mag = 10 ** Math.floor(Math.log10(Math.max(raw, 1)));
  const step = [1, 2, 5, 10].map((m) => m * mag).find((s) => s >= raw) || mag * 10;
  const ticks = [];
  for (let v = 0; v <= max + step * 0.001; v += step) ticks.push(v);
  return ticks;
}

// ---------------------------------------------------------------- shell
async function boot() {
  await loadExperiments();
  document.querySelectorAll(".tabs button").forEach((btn) => {
    btn.addEventListener("click", () => {
      state.tab = btn.dataset.tab;
      document.querySelectorAll(".tabs button").forEach((b) =>
        b.setAttribute("aria-selected", String(b === btn)));
      render();
    });
  });
  document.getElementById("experiment-select").addEventListener("change", async (e) => {
    state.expId = e.target.value;
    state.versionId = null;
    await loadDetail();
  });
  document.getElementById("version-select").addEventListener("change", async (e) => {
    state.versionId = Number(e.target.value);
    await loadDetail({ keepVersion: true });
  });
  document.getElementById("new-experiment").addEventListener("click", openNewExperiment);
  document.getElementById("theme-toggle").addEventListener("click", toggleTheme);
  applyTheme(localStorage.getItem("exp-theme"));
}

async function loadExperiments() {
  const data = await api("/api/experiments");
  state.experiments = data.experiments || [];
  const select = document.getElementById("experiment-select");
  if (!state.experiments.length) {
    select.innerHTML = `<option value="">No experiments</option>`;
    renderEmpty();
    return;
  }
  if (!state.expId || !state.experiments.some((e) => e.id === state.expId)) {
    state.expId = state.experiments[0].id;
  }
  select.innerHTML = state.experiments
    .map((e) => `<option value="${esc(e.id)}" ${e.id === state.expId ? "selected" : ""}>${esc(e.name)}</option>`)
    .join("");
  await loadDetail();
}

async function loadDetail({ keepVersion = false } = {}) {
  if (!state.expId) return renderEmpty();
  const detail = await api(`/api/experiments/${encodeURIComponent(state.expId)}`);
  state.detail = detail;
  const versions = detail.versions || [];
  if (versions.length) {
    state.versionId = state.versionId && versions.some((v) => v.id === state.versionId) && keepVersion
      ? state.versionId
      : versions[versions.length - 1].id;
    state.result = await api(`/api/experiments/${encodeURIComponent(state.expId)}/analysis?version=${state.versionId}`);
  }
  if (!keepVersion || !versions.some((v) => v.id === state.versionId)) {
    state.versionId = versions.length ? versions[versions.length - 1].id : null;
  }
  const vSelect = document.getElementById("version-select");
  vSelect.innerHTML = versions
    .slice()
    .reverse()
    .map((v) => `<option value="${v.id}" ${v.id === state.versionId ? "selected" : ""}>v${v.number} · ${esc(v.label)}${v.status !== "active" ? ` (${v.status.replace("_", " ")})` : ""}</option>`)
    .join("");
  render();
}

function renderEmpty() {
  document.getElementById("exp-title").textContent = "No experiments yet";
  document.getElementById("exp-subtitle").textContent = "Create one, or load the demo dataset, to explore analysis.";
  document.getElementById("hero-status").innerHTML = "";
  document.getElementById("view").innerHTML = `
    <div class="card empty">
      <p>No experiments found in this database.</p>
      <div class="row" style="justify-content:center;margin-top:14px">
        <button class="btn" id="empty-new">Create experiment</button>
        <button class="btn subtle" id="empty-demo">Load demo dataset</button>
      </div>
    </div>`;
  document.getElementById("empty-new").addEventListener("click", openNewExperiment);
  document.getElementById("empty-demo").addEventListener("click", async () => {
    try {
      await post("/api/demo", {});
      await loadExperiments();
    } catch (err) {
      alert(err.message);
    }
  });
}

function render() {
  if (!state.detail) return;
  const exp = state.detail.experiment;
  const version = state.detail.versions.find((v) => v.id === state.versionId) || state.detail.versions.at(-1);
  document.getElementById("exp-eyebrow").textContent =
    `${exp.allocation_method === "stratified" ? "Stratified random" : "Fixed split"}${exp.cross_device ? " · cross-device" : ""}${exp.allow_switch ? " · switching allowed" : " · switching blocked"}`;
  document.getElementById("exp-title").textContent = exp.name;
  document.getElementById("exp-subtitle").textContent = exp.description || "";
  const status = document.getElementById("hero-status");
  status.innerHTML = version
    ? `${statusBadge(version.status)} ${badge("neutral", `v${version.number}`)} ${badge("neutral", version.kind.replace("_", " "))}`
    : "";
  const view = document.getElementById("view");
  const renderers = {
    overview: renderOverview,
    balance: renderBalance,
    attrition: renderAttrition,
    metrics: renderMetrics,
    versions: renderVersions,
    checks: renderChecks,
  };
  view.innerHTML = "";
  (renderers[state.tab] || renderOverview)(view, version);
  bindTips(view);
}

// ---------------------------------------------------------------- overview
function renderOverview(root, version) {
  const result = state.result;
  const pop = result.populations;
  const diag = result.diagnostics;
  const itt = pop.intention_to_treat;
  const ast = pop.as_treated;
  const totalExcluded = diag.units_excluded;

  const tiles = el(`<div class="grid four"></div>`);
  tiles.innerHTML = [
    statTile("Units assigned", fmt.n(diag.units), `${fmt.n(diag.assignments_in_window)} assignments`),
    statTile("Eligible (ITT)", fmt.n(itt.units.control + itt.units.treatment), `${fmt.pct(fmtRatio(itt.units.control, itt.units.control + itt.units.treatment))} of eligible are control`),
    statTile("Exposed (as-treated)", fmt.n(ast.units.control + ast.units.treatment), `${fmt.pct(result.attrition.total.exposure_rate)} of units exposed`),
    statTile("Contaminated units excluded", fmt.n(totalExcluded), `${Object.keys(result.exclusions.by_rule).length} distinct rules`),
  ].join("");
  root.appendChild(tiles);

  const layout = el(`<div class="grid two"></div>`);
  const funnelCard = el(`<div class="card"><div class="card-head"><div><h2>Sample funnel</h2><p>Where units leave the analysis, from assignment to per-protocol.</p></div></div><div class="funnel-body"></div></div>`);
  funnelCard.querySelector(".funnel-body").innerHTML = hbarChart(
    result.funnel.map((row) => ({
      label: row.stage,
      value: row.count,
      className: "bar-neutral",
      tip: `${row.stage}: ${fmt.n(row.count)}${row.note ? `\n${row.note}` : ""}`,
    })),
    { width: 620, labelWidth: 236, valueFmt: fmt.n }
  ) + `<p class="small muted" style="margin-top:8px">Each stage is the population the next analysis runs on; hover a bar for the note.</p>`;
  layout.appendChild(funnelCard);

  const effectRows = itt.metrics.map((m) => {
    const isProp = m.type === "proportion";
    return {
      label: m.name,
      diff: m.effect.diff,
      ciLow: m.effect.ci_low,
      ciHigh: m.effect.ci_high,
      p: m.effect.p_value,
      diffLabel: isProp ? fmt.pp(m.effect.diff) : fmt.signed(m.effect.diff),
      ciLabel: isProp
        ? `[${fmt.pp(m.effect.ci_low)}, ${fmt.pp(m.effect.ci_high)}]`
        : `[${fmt.signed(m.effect.ci_low)}, ${fmt.signed(m.effect.ci_high)}]`,
    };
  });
  const forestCard = el(`<div class="card"><div class="card-head"><div><h2>ITT effect per metric</h2><p>Treatment − control with 95% CI. Filled mark = interval excludes zero.</p></div>${badge("neutral", "ITT")}</div><div class="forest"></div></div>`);
  forestCard.querySelector(".forest").innerHTML = forestPlot(effectRows, { width: 620 }) +
    `<p class="small muted" style="margin-top:6px">As-treated estimates are on the Metric differences tab.</p>`;
  layout.appendChild(forestCard);
  root.appendChild(layout);

  const checkCard = el(`<div class="card"><div class="card-head"><div><h2>Verification snapshot</h2><p>Repeated exposure, late results and balance checks for this version.</p></div>
    <button class="btn subtle" id="go-checks">Open verification</button></div><div class="grid three" id="check-grid"></div></div>`);
  checkCard.querySelector("#check-grid").innerHTML = result.checks
    .slice(0, 6)
    .map((c) => `<div class="stack" style="gap:4px">${badge(c.status, c.label)}<span class="value small">${fmtCheckValue(c)}</span><span class="small muted">${esc(c.detail)}</span></div>`)
    .join("");
  root.appendChild(checkCard);
  checkCard.querySelector("#go-checks").addEventListener("click", () => {
    document.querySelector('.tabs button[data-tab="checks"]').click();
  });
}

function statTile(label, value, hint) {
  return `<div class="card stat"><span class="label">${esc(label)}</span><span class="value">${value}</span><span class="hint">${esc(hint)}</span></div>`;
}

function fmtRatio(a, b) {
  if (!b) return null;
  return a / b;
}

function fmtCheckValue(c) {
  if (Array.isArray(c.value)) return esc(c.value.join(", "));
  if (typeof c.value === "number") {
    if (c.key === "sample_ratio") return `p=${fmt.pval(c.value)}`;
    if (c.key === "allocation_integrity") return `${fmt.pct(c.value, 1)} mismatch`;
    return fmt.n(c.value);
  }
  return esc(String(c.value ?? ""));
}

// ---------------------------------------------------------------- balance
function renderBalance(root, version) {
  const result = state.result;
  const b = result.balance;
  const srm = b.srm;

  const head = el(`<div class="card"><div class="card-head"><div><h2>Randomisation balance</h2>
    <p>${b.allocation_method === "stratified" ? "Stratified split" : "Fixed split"} · ${esc(result.experiment.allocation_method)} · ITT population.</p></div>
    ${badge(srm.alert ? "fail" : "pass", srm.alert ? "Sample ratio mismatch" : "Ratio holds")}</div>
    <div class="grid four" id="balance-tiles"></div></div>`);
  head.querySelector("#balance-tiles").innerHTML = [
    statTile("Analysed units", fmt.n(srm.n), "ITT population"),
    statTile("Treatment share", fmt.pct(srm.observed_treatment_share, 2), `expected ${fmt.pct(srm.expected_treatment_share, 0)}`),
    statTile("SRM p-value", fmt.pval(srm.p_value), `χ² ${fmt.num(srm.statistic, 2)} · df ${srm.df}`),
    statTile("Re-allocation mismatch", fmt.pct(b.allocation_integrity.mismatch_rate, 2), `${fmt.n(b.allocation_integrity.checked)} checked`),
  ].join("");
  root.appendChild(head);

  const armCard = el(`<div class="card"><div class="card-head"><div><h2>Arm split</h2><p>Observed against the expected treatment share.</p></div>
    <div class="legend"><span><span class="swatch" style="background:var(--series-control)"></span>Control</span><span><span class="swatch" style="background:var(--series-treatment)"></span>Treatment</span></div></div>
    <div id="arm-chart"></div></div>`);
  armCard.querySelector("#arm-chart").innerHTML = groupedBars([
    {
      label: "observed",
      short: "Observed",
      control: result.populations.intention_to_treat.units.control,
      treatment: result.populations.intention_to_treat.units.treatment,
    },
    {
      label: "expected",
      short: "Expected",
      control: Math.round(srm.n * (1 - srm.expected_treatment_share)),
      treatment: Math.round(srm.n * srm.expected_treatment_share),
    },
  ], { width: 620, height: 210 });
  root.appendChild(armCard);

  if (b.strata.length && b.allocation_method === "stratified") {
    const strataCard = el(`<div class="card"><div class="card-head"><div><h2>Strata</h2><p>Arm sizes per stratum with the per-stratum test. Alerts need p &lt; 0.001 and at least 30 units.</p></div></div>
      <div class="table-wrap"><table class="tbl-small"><thead><tr>
      <th>Stratum</th><th class="num">Control</th><th class="num">Treatment</th><th class="num">Observed T share</th>
      <th class="num">Expected</th><th class="num">p</th><th>Status</th></tr></thead><tbody>
      ${b.strata.map((s) => `<tr>
        <td class="mono">${esc(s.stratum)}</td><td class="num">${fmt.n(s.control)}</td><td class="num">${fmt.n(s.treatment)}</td>
        <td class="num">${fmt.pct(s.observed_treatment_share)}</td><td class="num">${fmt.pct(s.expected_treatment_share)}</td>
        <td class="num">${fmt.pval(s.p_value)}</td><td>${badge(s.alert ? "fail" : "pass", s.alert ? "imbalance" : "ok")}</td></tr>`).join("")}
      </tbody></table></div></div>`);
    root.appendChild(strataCard);
  }

  if (b.covariates.length) {
    const rows = b.covariates.map((c) => ({
      label: `${c.column} = ${c.level}`,
      smd: c.smd,
      alert: c.alert,
    }));
    const covCard = el(`<div class="card"><div class="card-head"><div><h2>Covariate balance</h2><p>Standardized mean difference per level; the band marks ±0.10. Rows are flagged only when the level has at least 30 units.</p></div></div>
      <div class="cov-plot"></div>
      <details style="margin-top:10px"><summary>Table view</summary><div class="table-wrap"><table class="tbl-small"><thead><tr>
      <th>Column</th><th>Level</th><th class="num">Control</th><th class="num">Treatment</th><th class="num">Share C</th><th class="num">Share T</th><th class="num">SMD</th><th class="num">p</th></tr></thead><tbody>
      ${b.covariates.map((c) => `<tr><td>${esc(c.column)}</td><td>${esc(c.level)}</td>
        <td class="num">${fmt.n(c.control)}</td><td class="num">${fmt.n(c.treatment)}</td>
        <td class="num">${fmt.pct(c.share_control)}</td><td class="num">${fmt.pct(c.share_treatment)}</td>
        <td class="num ${c.alert ? "label-strong" : ""}">${fmt.num(c.smd, 2)}</td><td class="num">${fmt.pval(c.p_value)}</td></tr>`).join("")}
      </tbody></table></div></details></div>`);
    covCard.querySelector(".cov-plot").innerHTML = smdPlot(rows, { width: 620 });
    root.appendChild(covCard);
  }

  const integrity = b.allocation_integrity;
  const intCard = el(`<div class="card"><div class="card-head"><div><h2>Allocation integrity</h2>
    <p>Every ingested initial assignment is recomputed from the deterministic splitter. Mismatches mean the warehouse and the splitter disagree.</p></div>
    ${badge(integrity.alert ? "fail" : "pass", integrity.alert ? "Splitter mismatch" : "Reproducible")}</div>
    <div class="row"><span class="secondary">Checked</span><strong>${fmt.n(integrity.checked)}</strong>
    <span class="secondary">Mismatches</span><strong>${fmt.n(integrity.mismatches)}</strong>
    <span class="secondary">Rate</span><strong>${fmt.pct(integrity.mismatch_rate, 2)}</strong></div>
    ${integrity.examples.length ? `<div class="table-wrap" style="margin-top:12px"><table class="tbl-small"><thead><tr><th>User</th><th>Assignment</th><th>Ingested</th><th>Recomputed</th></tr></thead><tbody>
      ${integrity.examples.map((m) => `<tr><td class="mono">${esc(m.user_id)}</td><td class="mono">${esc(m.assignment_id)}</td><td>${esc(m.ingested)}</td><td>${esc(m.recomputed)}</td></tr>`).join("")}
    </tbody></table></div>` : ""}</div>`);
  root.appendChild(intCard);
}

// ---------------------------------------------------------------- attrition
function renderAttrition(root, version) {
  const result = state.result;
  const attr = result.attrition;
  const arms = attr.by_arm;

  const tiles = el(`<div class="grid four"></div>`);
  tiles.innerHTML = [
    statTile("Overall exposure rate", fmt.pct(attr.total.exposure_rate, 1), `${fmt.n(attr.total.exposed)} of ${fmt.n(attr.total.assigned)} units`),
    statTile("Control exposure", fmt.pct(arms.control.exposure_rate, 1), `${fmt.n(arms.control.no_exposure)} without exposure`),
    statTile("Treatment exposure", fmt.pct(arms.treatment.exposure_rate, 1), `${fmt.n(arms.treatment.no_exposure)} without exposure`),
    statTile("Differential exposure", fmt.pp(attr.differential_exposure_rate), "treatment − control exposure rate"),
  ].join("");
  root.appendChild(tiles);

  const diff = attr.differential_exposure_rate;
  if (diff !== null && Math.abs(diff) > 0.02) {
    const warn = el(`<div class="callout warn"><strong>Differential attrition.</strong> Exposure rates differ by ${fmt.pp(diff)} between arms. If exposure is a post-assignment behaviour, the as-treated estimate is comparing different populations — read it next to ITT.</div>`);
    root.appendChild(warn);
  }

  const chartCard = el(`<div class="card"><div class="card-head"><div><h2>Sample flow by arm</h2><p>Assigned → eligible → exposed. Units lost at each step keep a reason in the ledger below.</p></div>
    <div class="legend"><span><span class="swatch" style="background:var(--series-control)"></span>Control</span><span><span class="swatch" style="background:var(--series-treatment)"></span>Treatment</span></div></div>
    <div id="attrition-chart"></div></div>`);
  chartCard.querySelector("#attrition-chart").innerHTML = groupedBars([
    { label: "Assigned", short: "Assigned", control: arms.control.assigned, treatment: arms.treatment.assigned },
    { label: "Eligible", short: "Eligible", control: arms.control.eligible, treatment: arms.treatment.eligible },
    { label: "Exposed", short: "Exposed", control: arms.control.exposed, treatment: arms.treatment.exposed },
  ], { width: 620, height: 230 });
  root.appendChild(chartCard);

  const table = el(`<div class="card"><div class="card-head"><div><h2>Units lost from the exposed population</h2><p>Shows ${Math.min(attr.lost_units.length, 200)} of ${fmt.n(attr.total.assigned - attr.total.exposed)} units without an exposure event.</p></div></div>
    <div class="table-wrap"><table class="tbl-small"><thead><tr><th>User</th><th>Initial arm</th><th>Reason</th><th>Exclusion rules</th></tr></thead><tbody>
    ${attr.lost_units.map((u) => `<tr><td class="mono">${esc(u.user_id)}</td><td>${esc(u.variant)}</td>
      <td>${u.reason === "excluded" ? badge("warn", "excluded") : badge("neutral", "no exposure event")}</td>
      <td>${u.rules.length ? u.rules.map((r) => `<code>${esc(r)}</code>`).join(" ") : '<span class="muted">—</span>'}</td></tr>`).join("")}
    </tbody></table></div></div>`);
  if (!attr.lost_units.length) table.querySelector("tbody").innerHTML = `<tr><td colspan="4" class="muted">No units were lost.</td></tr>`;
  root.appendChild(table);

  const recon = el(`<div class="card"><div class="card-head"><div><h2>Same units, three populations</h2><p>Arm sizes for each definition. Divergence between ITT and as-treated is the sample-loss effect.</p></div></div>
    <div class="table-wrap"><table><thead><tr><th>Population</th><th class="num">Control</th><th class="num">Treatment</th><th class="num">Total</th><th>Definition</th></tr></thead><tbody>
    ${Object.entries(result.populations).map(([key, p]) => `<tr>
      <td><strong>${esc(p.label)}</strong></td><td class="num">${fmt.n(p.units.control)}</td>
      <td class="num">${fmt.n(p.units.treatment)}</td><td class="num">${fmt.n(p.units.control + p.units.treatment)}</td>
      <td class="small secondary">${esc(p.definition)}</td></tr>`).join("")}
    </tbody></table></div></div>`);
  root.appendChild(recon);
}

// ---------------------------------------------------------------- metrics
function renderMetrics(root, version) {
  const result = state.result;
  const popKey = state.population;
  const pop = result.populations[popKey];

  const controls = el(`<div class="card"><div class="card-head"><div><h2>Metric differences</h2>
    <p>${esc(pop.definition)}</p></div>
    <div class="row" role="group" aria-label="Population">
      ${POPULATIONS.map(([key, label]) => `<button class="btn ${key === popKey ? "" : "subtle"}" data-pop="${key}" type="button">${label}</button>`).join("")}
    </div></div>
    <div class="row small secondary">
      <span>Control n=${fmt.n(pop.units.control)}</span><span>Treatment n=${fmt.n(pop.units.treatment)}</span>
      ${pop.notes.length ? `<span>· ${esc(pop.notes.join(" "))}</span>` : ""}
    </div>
    <div id="metric-forest" style="margin-top:14px"></div></div>`);
  const rows = pop.metrics.map((m) => {
    const isProp = m.type === "proportion";
    return {
      label: m.name,
      diff: m.effect.diff,
      ciLow: m.effect.ci_low,
      ciHigh: m.effect.ci_high,
      p: m.effect.p_value,
      diffLabel: isProp ? fmt.pp(m.effect.diff) : fmt.signed(m.effect.diff),
      ciLabel: isProp
        ? `[${fmt.pp(m.effect.ci_low)}, ${fmt.pp(m.effect.ci_high)}]`
        : `[${fmt.signed(m.effect.ci_low, 3)}, ${fmt.signed(m.effect.ci_high, 3)}]`,
    };
  });
  controls.querySelector("#metric-forest").innerHTML = forestPlot(rows, { width: 760 });
  controls.querySelectorAll("[data-pop]").forEach((btn) => {
    btn.addEventListener("click", () => {
      state.population = btn.dataset.pop;
      render();
    });
  });
  root.appendChild(controls);

  const table = el(`<div class="card"><div class="card-head"><div><h2>Estimate table</h2><p>Proportions use a two-proportion z-test with arm size as denominator; continuous metrics use Welch's t-test.</p></div></div>
    <div class="table-wrap"><table><thead><tr>
      <th>Metric</th><th class="num">Control</th><th class="num">Treatment</th><th>Effect (T − C)</th><th class="num">Relative</th><th class="num">p</th><th>Estimator</th><th>Definition</th>
    </tr></thead><tbody>
    ${pop.metrics.map((m) => {
      const isProp = m.type === "proportion";
      const c = isProp ? `${fmt.n(m.control.successes)} / ${fmt.n(m.control.n)}` : `${fmt.num(m.control.value, 2)} ± ${fmt.num(m.control.sd, 2)}`;
      const t = isProp ? `${fmt.n(m.treatment.successes)} / ${fmt.n(m.treatment.n)}` : `${fmt.num(m.treatment.value, 2)} ± ${fmt.num(m.treatment.sd, 2)}`;
      return `<tr>
        <td><strong>${esc(m.name)}</strong><div class="small muted"><code>${esc(m.metric)}</code> · ${esc(m.type)}</div></td>
        <td class="num">${c}</td><td class="num">${t}</td>
        <td>${effectCell(m)}</td>
        <td class="num">${m.effect.relative === null ? "—" : fmt.pct(m.effect.relative, 1)}</td>
        <td class="num">${fmt.pval(m.effect.p_value)}</td>
        <td class="small secondary">${esc(m.estimator)}</td>
        <td class="small secondary">${m.definition_changed_at_version ? badge("warn", `changed in v${m.definition_changed_at_version}`) : badge("neutral", `v${m.metric_version}`)}</td>
      </tr>`;
    }).join("")}
    </tbody></table></div></div>`);
  root.appendChild(table);

  // ITT vs as-treated side by side: the point of the tool.
  const cmp = el(`<div class="card"><div class="card-head"><div><h2>ITT vs actual exposure</h2><p>The same metrics under the two estimands. A large gap means exposure, not randomisation, is driving the difference.</p></div></div>
    <div class="table-wrap"><table><thead><tr><th>Metric</th><th class="num">ITT diff</th><th class="num">As-treated diff</th><th class="num">Gap</th><th>Read</th></tr></thead><tbody>
    ${result.populations.intention_to_treat.metrics.map((m) => {
      const a = result.populations.as_treated.metrics.find((x) => x.metric === m.metric);
      const gap = a && a.effect.diff !== null && m.effect.diff !== null ? a.effect.diff - m.effect.diff : null;
      const base = Math.max(1e-9, Math.abs(m.effect.diff || 0));
      const large = gap !== null && Math.abs(gap) > 0.02 && Math.abs(gap) / base > 0.5;
      return `<tr><td><strong>${esc(m.name)}</strong></td>
        <td class="num">${m.effect.diff === null ? "—" : fmt.pp(m.effect.diff)}</td>
        <td class="num">${a && a.effect.diff !== null ? fmt.pp(a.effect.diff) : "—"}</td>
        <td class="num ${large ? "label-strong" : ""}">${gap === null ? "—" : fmt.pp(gap)}</td>
        <td class="small">${large ? badge("warn", "attribution sensitive") : badge("pass", "consistent")}</td></tr>`;
    }).join("")}
    </tbody></table></div></div>`);
  root.appendChild(cmp);
}

// ---------------------------------------------------------------- versions
function renderVersions(root, version) {
  const detail = state.detail;
  const versions = detail.versions;
  const versionsSorted = versions.slice().sort((a, b) => b.number - a.number);

  const actions = el(`<div class="card"><div class="card-head"><div><h2>Version timeline</h2>
    <p>Pausing and rolling back freeze the sample at the transition; config and metric changes open a new evaluation.</p></div>
    <div class="row">
      <button class="btn subtle" id="act-pause" type="button">Pause</button>
      <button class="btn subtle" id="act-resume" type="button">Resume</button>
      <button class="btn subtle" id="act-metric" type="button">Change metric definition</button>
      <button class="btn subtle" id="act-rollback" type="button">Roll back</button>
      <button class="btn subtle" id="act-config" type="button">Config change</button>
    </div></div>
    <div class="table-wrap"><table><thead><tr>
      <th>Version</th><th>Kind</th><th>Status</th><th>Sample window</th><th>Lifecycle</th>
      <th class="num">Units</th><th class="num">Excluded</th><th>Frozen at</th><th>Reason</th>
    </tr></thead><tbody>
    ${versionsSorted.map((v) => {
      const counts = v.counts || {};
      return `<tr>
        <td><strong>v${v.number}</strong><div class="small muted">${esc(v.label)}</div></td>
        <td class="small">${esc(v.kind.replace("_", " "))}${v.window_mode === "rollback_base" ? '<div class="small muted">replays base window</div>' : ""}</td>
        <td>${statusBadge(v.status)}</td>
        <td class="small mono-num">${fmt.time(v.window_start)}<br>→ ${v.window_end ? fmt.time(v.window_end) : '<span class="muted">open</span>'}</td>
        <td class="small mono-num muted">${fmt.time(v.effective_from)}<br>→ ${v.effective_to ? fmt.time(v.effective_to) : "—"}</td>
        <td class="num">${fmt.n(counts.units)}</td>
        <td class="num">${fmt.n(counts.units_excluded)}</td>
        <td class="small">${v.frozen_at ? esc(fmt.time(v.frozen_at)) : '<span class="muted">not frozen</span>'}</td>
        <td class="small secondary">${esc(v.reason || "—")}</td></tr>`;
    }).join("")}
    </tbody></table></div></div>`);
  root.appendChild(actions);

  const hooks = {
    "act-pause": () => openVersionDialog("pause"),
    "act-resume": () => openVersionDialog("resume"),
    "act-metric": () => openVersionDialog("metric_change"),
    "act-rollback": () => openVersionDialog("rollback"),
    "act-config": () => openVersionDialog("config_change"),
  };
  Object.entries(hooks).forEach(([id, fn]) => actions.querySelector(`#${id}`).addEventListener("click", fn));

  const allVersions = versions;
  if (allVersions.length < 2) {
    const note = el(`<div class="card"><div class="card-head"><div><h2>Version comparison</h2><p>Open a second version (pause, config or metric change) to compare estimates side by side.</p></div></div></div>`);
    root.appendChild(note);
  } else {
    const cmpCard = el(`<div class="card"><div class="card-head"><div><h2>Version comparison</h2><p>Frozen-sample estimates under each version's rules. Read the warnings before treating a delta as a change in effect.</p></div></div>
      <div class="row" style="margin-bottom:12px">
        <label class="field-inline"><span>Left</span><select id="cmp-left">${allVersions.slice().reverse().map((v) => `<option value="${v.id}">v${v.number} · ${esc(v.label)}</option>`).join("")}</select></label>
        <label class="field-inline"><span>Right</span><select id="cmp-right">${allVersions.slice().reverse().map((v) => `<option value="${v.id}" ${v.id === state.versionId ? "selected" : ""}>v${v.number} · ${esc(v.label)}</option>`).join("")}</select></label>
        <button class="btn" id="cmp-run" type="button">Compare</button>
      </div><div id="cmp-out"></div></div>`);
    root.appendChild(cmpCard);
    const leftSel = cmpCard.querySelector("#cmp-left");
    const rightSel = cmpCard.querySelector("#cmp-right");
    leftSel.value = allVersions[allVersions.length - 2].id;
    rightSel.value = allVersions[allVersions.length - 1].id;
    const run = async () => {
      const out = cmpCard.querySelector("#cmp-out");
      out.innerHTML = `<p class="muted">Comparing…</p>`;
      try {
        const cmp = await api(`/api/experiments/${encodeURIComponent(state.expId)}/compare?left=${leftSel.value}&right=${rightSel.value}`);
        out.innerHTML = comparisonHtml(cmp);
        bindTips(out);
        out.querySelectorAll("[data-goto]").forEach((b) => b.addEventListener("click", () => {
          state.versionId = Number(b.dataset.goto);
          document.getElementById("version-select").value = b.dataset.goto;
          loadDetail({ keepVersion: true });
        }));
      } catch (err) {
        out.innerHTML = `<div class="callout fail">${esc(err.message)}</div>`;
      }
    };
    cmpCard.querySelector("#cmp-run").addEventListener("click", run);
    run();
  }

  // Snapshot ledger
  const snapCard = el(`<div class="card"><div class="card-head"><div><h2>Sample snapshots</h2>
    <p>Each version stores the exact assignment and event ids it analysed plus a content hash, so a later change is detectable.</p></div>
    <button class="btn subtle" id="verify-current" type="button">Verify current version</button></div>
    <div id="verify-out"></div>
    <div class="table-wrap" style="margin-top:12px"><table class="tbl-small"><thead><tr>
      <th>Version</th><th class="num">Assignments</th><th class="num">Events</th><th class="num">Excluded units</th>
      <th>Exclusions</th><th>Event hash</th><th>Config hash</th><th></th>
    </tr></thead><tbody>
    ${versionsSorted.map((v) => {
      const s = v.snapshot || {};
      return `<tr>
        <td><strong>v${v.number}</strong></td>
        <td class="num">${fmt.n(s.counts?.assignments)}</td>
        <td class="num">${fmt.n(s.counts?.events)}</td>
        <td class="num">${fmt.n(s.counts?.units_excluded)}</td>
        <td class="small">${Object.entries(s.exclusions_by_rule || {}).map(([k, n]) => `<code>${esc(k)}:${n}</code>`).join(" ") || '<span class="muted">none</span>'}</td>
        <td class="mono small muted">${esc((s.event_ids_hash || "").slice(0, 10))}…</td>
        <td class="mono small muted">${esc((s.config_hash || "").slice(0, 10))}…</td>
        <td><button class="btn subtle small" data-verify="${v.id}" type="button">Verify</button></td></tr>`;
    }).join("")}
    </tbody></table></div></div>`);
  root.appendChild(snapCard);

  const verifyInto = async (versionId) => {
    const out = snapCard.querySelector("#verify-out");
    out.innerHTML = `<p class="muted">Verifying…</p>`;
    try {
      const rep = await api(`/api/experiments/${encodeURIComponent(state.expId)}/verify?version=${versionId}`);
      rep._requestedVersion = versionId;
      out.innerHTML = verifyHtml(rep);
      bindTips(out);
    } catch (err) {
      out.innerHTML = `<div class="callout fail">${esc(err.message)}</div>`;
    }
  };
  snapCard.querySelector("#verify-current").addEventListener("click", () => verifyInto(state.versionId));
  snapCard.querySelectorAll("[data-verify]").forEach((b) =>
    b.addEventListener("click", () => verifyInto(b.dataset.verify)));
}

function comparisonHtml(cmp) {
  const warn = cmp.warnings.length
    ? `<div class="callout warn"><strong>Read before comparing.</strong><ul class="clean">${cmp.warnings.map((w) => `<li>${esc(w)}</li>`).join("")}</ul></div>`
    : `<div class="callout">Windows do not overlap and exclusions match — the two samples are directly comparable.</div>`;
  return `${warn}
    <div class="grid two" style="margin-top:12px">
      <div class="card" style="background:var(--surface-1)">
        <div class="row">${statusBadge(cmp.left.status)}<strong>v${cmp.left.version_number} · ${esc(cmp.left.label)}</strong></div>
        <div class="small secondary" style="margin-top:6px">units ${fmt.n(cmp.left.counts?.units)} · events ${fmt.n(cmp.left.counts?.events)} · excluded ${fmt.n(cmp.left.counts?.units_excluded)}</div>
        <div class="small muted">${fmt.time(cmp.left.window.start)} → ${cmp.left.window.end ? fmt.time(cmp.left.window.end) : "open"}</div>
      </div>
      <div class="card" style="background:var(--surface-1)">
        <div class="row">${statusBadge(cmp.right.status)}<strong>v${cmp.right.version_number} · ${esc(cmp.right.label)}</strong></div>
        <div class="small secondary" style="margin-top:6px">units ${fmt.n(cmp.right.counts?.units)} · events ${fmt.n(cmp.right.counts?.events)} · excluded ${fmt.n(cmp.right.counts?.units_excluded)}</div>
        <div class="small muted">${fmt.time(cmp.right.window.start)} → ${cmp.right.window.end ? fmt.time(cmp.right.window.end) : "open"}</div>
      </div>
    </div>
    <div class="table-wrap" style="margin-top:12px"><table><thead><tr>
      <th>Metric</th><th class="num">v${cmp.left.version_number} ITT</th><th class="num">v${cmp.right.version_number} ITT</th>
      <th class="num">Δ</th><th class="num">As-treated L / R</th><th>Definition</th>
    </tr></thead><tbody>
    ${cmp.metrics.map((m) => {
      const changed = m.metric_version_left !== m.metric_version_right;
      return `<tr><td><strong>${esc(m.name)}</strong></td>
        <td class="num">${m.left_diff === null ? "—" : fmt.pp(m.left_diff)}</td>
        <td class="num">${m.right_diff === null ? "—" : fmt.pp(m.right_diff)}</td>
        <td class="num ${m.delta !== null && Math.abs(m.delta) > 0.01 ? "label-strong" : ""}">${m.delta === null ? "—" : fmt.pp(m.delta)}</td>
        <td class="num small">${m.as_treated_left === null ? "—" : fmt.pp(m.as_treated_left)} / ${m.as_treated_right === null ? "—" : fmt.pp(m.as_treated_right)}</td>
        <td>${changed ? badge("warn", `v${m.metric_version_left}→v${m.metric_version_right}`) : badge("neutral", "unchanged")}</td></tr>`;
    }).join("")}
    </tbody></table></div>`;
}

function verifyHtml(rep) {
  const ok = rep.verdict === "clean";
  const requested = state.detail.versions.find((v) => v.id === rep._requestedVersion);
  const active = requested ? requested.status === "active" : rep.snapshot_is_frozen === false;
  const head = active
    ? `<div class="callout"><strong>This version is still active.</strong> Its snapshot is refreshed on every recompute, so a clean result only means the stored ids agree with the current data — it is not an independent frozen record yet. Pause or roll back the experiment to freeze it.</div>`
    : ok
    ? `<div class="callout"><strong>Snapshot verified.</strong> No assignments or events changed since it was frozen at ${esc(fmt.time(rep.frozen_at))}.</div>`
    : `<div class="callout warn"><strong>Drift detected.</strong> ${fmt.n(rep.new_event_count)} new event(s), ${fmt.n(rep.removed_event_count)} removed, ${fmt.n(rep.new_assignment_count)} new assignment(s), ${fmt.n(rep.removed_assignment_count)} removed since the freeze at ${esc(fmt.time(rep.version.frozen_at))}.</div>`;
  const shift = rep.metric_shift.map((m) => `<tr><td>${esc(m.metric)}</td>
    <td class="num">${m.frozen_itt_diff === null ? "—" : fmt.pp(m.frozen_itt_diff)}</td>
    <td class="num">${m.current_itt_diff === null ? "—" : fmt.pp(m.current_itt_diff)}</td>
    <td class="num">${m.delta === null ? "—" : fmt.pp(m.delta)}</td></tr>`).join("");
  const late = rep.late_results || {};
  const lag = late.late_lag || late.lag || {};
  const lateExamples = (late.examples || []).map((e) => `<tr><td class="mono">${esc(e.event_id)}</td><td>${esc(e.metric_key)}</td>
    <td class="small">${esc(fmt.time(e.occurred_at))}</td><td class="small">${esc(fmt.time(e.received_at))}</td>
    <td class="small">${esc(e.reason.replaceAll("_", " "))}</td></tr>`).join("");
  return `${head}
    <div class="grid two" style="margin-top:12px">
      <div><h3>Frozen vs live estimate (ITT)</h3><div class="table-wrap"><table class="tbl-small"><thead><tr><th>Metric</th><th class="num">Frozen</th><th class="num">Live</th><th class="num">Δ</th></tr></thead><tbody>${shift}</tbody></table></div></div>
      <div><h3>Late-arriving results</h3>
        <p class="small secondary">${fmt.n(rep.new_event_count)} event(s) arrived after the version window closed. Median delay from occurrence to receipt ${esc(fmt.dur(lag.median_seconds))}, p95 ${esc(fmt.dur(lag.p95_seconds))}, max ${esc(fmt.dur(lag.max_seconds))}.</p>
        <div class="table-wrap"><table class="tbl-small"><thead><tr><th>Event</th><th>Metric</th><th>Occurred</th><th>Received</th><th>Reason</th></tr></thead>
        <tbody>${lateExamples || '<tr><td colspan="5" class="muted">None.</td></tr>'}</tbody></table></div></div>
    </div>
    ${!ok ? `<details style="margin-top:10px"><summary>New ids</summary><p class="small mono">${rep.new_events.map(esc).join(", ") || "—"}</p></details>` : ""}`;
}

// ---------------------------------------------------------------- checks
function renderChecks(root, version) {
  const result = state.result;
  const checks = result.checks;
  const failing = checks.filter((c) => c.status === "fail");
  const warning = checks.filter((c) => c.status === "warn");

  const summary = el(`<div class="grid four"></div>`);
  summary.innerHTML = [
    statTile("Checks passed", fmt.n(checks.filter((c) => c.status === "pass").length), `${checks.length} total`),
    statTile("Warnings", fmt.n(warning.length), warning.map((c) => c.label).join(", ") || "none"),
    statTile("Failures", fmt.n(failing.length), failing.map((c) => c.label).join(", ") || "none"),
    statTile("Units with repeated exposure", fmt.n(result.diagnostics.units_multi_assignment), `${fmt.n(result.diagnostics.units_cross_variant)} switched arms · ${fmt.n(result.diagnostics.units_cross_device)} multi-device`),
  ].join("");
  root.appendChild(summary);

  const grid = el(`<div class="grid two"></div>`);
  checks.forEach((c) => {
    const card = el(`<div class="card"><div class="card-head"><div><h3>${esc(c.label)}</h3></div>${badge(c.status, c.status)}</div>
      <p class="secondary" style="margin-bottom:10px">${esc(c.detail)}</p><div class="check-body"></div></div>`);
    const body = card.querySelector(".check-body");
    if (c.items && c.items.length) {
      body.innerHTML = `<div class="table-wrap"><table class="tbl-small"><thead><tr><th>User</th><th class="num">Assignments</th><th>Arms</th><th>Devices</th><th>Exclusion</th></tr></thead><tbody>
        ${c.items.map((u) => `<tr><td class="mono">${esc(u.user_id)}</td><td class="num">${fmt.n(u.assignments)}</td>
          <td class="small">${u.variants.map((v) => `<span class="badge neutral">${esc(v)}</span>`).join(" ")}</td>
          <td class="small">${(u.devices || []).map(esc).join(", ") || "—"}</td>
          <td class="small">${(u.rules || []).map((r) => `<code>${esc(r)}</code>`).join(" ") || "—"}</td></tr>`).join("")}
      </tbody></table></div>`;
    } else if (c.lag) {
      body.innerHTML = `<div class="row small secondary"><span>Median lag <strong>${esc(fmt.dur(c.lag.median_seconds))}</strong></span>
        <span>p95 <strong>${esc(fmt.dur(c.lag.p95_seconds))}</strong></span>
        <span>max <strong>${esc(fmt.dur(c.lag.max_seconds))}</strong></span>
        <span>out-of-order <strong>${fmt.n(c.lag.negative)}</strong></span></div>`;
    } else {
      body.innerHTML = `<div class="row small secondary"><span>Value</span><strong>${esc(fmtCheckValue(c))}</strong></div>`;
    }
    grid.appendChild(card);
  });
  root.appendChild(grid);

  // Exclusion ledger
  const exclusions = result.exclusions;
  const ruleRows = Object.entries(exclusions.by_rule).map(([rule, count]) => {
    const examples = exclusions.unit_level.filter((u) => u.rule === rule).slice(0, 3);
    const eventExamples = exclusions.event_level.filter((e) => e.rule === rule).slice(0, 2);
    return `<tr><td><code>${esc(rule)}</code></td><td class="num">${fmt.n(count)}</td>
      <td class="small">${esc((examples[0] || eventExamples[0] || {}).reason || "—")}</td>
      <td class="small">${examples.map((e) => esc(e.user_id)).concat(eventExamples.map((e) => esc(e.event_id))).join(", ") || "—"}</td></tr>`;
  }).join("");
  const ledger = el(`<div class="card"><div class="card-head"><div><h2>Exclusion ledger</h2>
    <p>Every excluded sample carries a rule and a written reason; nothing is dropped silently.</p></div>
    ${badge(exclusions.unit_level.length || exclusions.event_level.length ? "info" : "pass", `${fmt.n(exclusions.unit_level.length)} units · ${fmt.n(exclusions.event_level.length)} events`)}</div>
    <div class="table-wrap"><table><thead><tr><th>Rule</th><th class="num">Count</th><th>Reason</th><th>Examples</th></tr></thead><tbody>
    ${ruleRows || '<tr><td colspan="4" class="muted">No exclusions.</td></tr>'}
    </tbody></table></div>
    <details style="margin-top:12px"><summary>Unit-level detail</summary><div class="table-wrap"><table class="tbl-small"><thead><tr><th>User</th><th>Rule</th><th>Reason</th><th>Detail</th></tr></thead><tbody>
    ${exclusions.unit_level.slice(0, 200).map((u) => `<tr><td class="mono">${esc(u.user_id)}</td><td><code>${esc(u.rule)}</code></td>
      <td class="small">${esc(u.reason)}</td><td class="small mono muted">${esc(JSON.stringify(u.detail))}</td></tr>`).join("") || '<tr><td colspan="4" class="muted">None.</td></tr>'}
    </tbody></table></div></details>
    <details style="margin-top:8px"><summary>Event-level detail</summary><div class="table-wrap"><table class="tbl-small"><thead><tr><th>Event</th><th>Assignment</th><th>Rule</th><th>Reason</th></tr></thead><tbody>
    ${exclusions.event_level.slice(0, 200).map((e) => `<tr>
      <td class="mono">${esc(e.detail?.event_id ?? "—")}</td>
      <td class="mono">${esc(e.assignment_id ?? "—")}</td><td><code>${esc(e.rule)}</code></td><td class="small">${esc(e.reason)}</td></tr>`).join("") || '<tr><td colspan="4" class="muted">None.</td></tr>'}
    </tbody></table></div></details></div>`);
  root.appendChild(ledger);

  const late = result.late_results;
  const lateCard = el(`<div class="card"><div class="card-head"><div><h2>Late and out-of-order results</h2>
    <p>Events whose receipt time falls after the version window are held out of the frozen sample and counted here.</p></div></div>
    <div class="grid three">
      ${statTile("Occurred after close", fmt.n(late.events_occurred_after_close), "excluded from the frozen sample")}
      ${statTile("Received after close", fmt.n(late.events_received_after_close), "arrived late, excluded")}
      ${statTile("Ingestion lag p95", esc(fmt.dur((late.late_lag || late.lag)?.p95_seconds)), `median ${esc(fmt.dur((late.late_lag || late.lag)?.median_seconds))}`)}
    </div>
    <div class="table-wrap" style="margin-top:12px"><table class="tbl-small"><thead><tr><th>Event</th><th>Metric</th><th>Occurred</th><th>Received</th><th>Reason</th></tr></thead><tbody>
    ${(late.examples || []).map((e) => `<tr><td class="mono">${esc(e.event_id)}</td><td>${esc(e.metric_key)}</td>
      <td class="small">${esc(fmt.time(e.occurred_at))}</td><td class="small">${esc(fmt.time(e.received_at))}</td>
      <td class="small">${esc(e.reason.replaceAll("_", " "))}</td></tr>`).join("") || '<tr><td colspan="5" class="muted">No late results.</td></tr>'}
    </tbody></table></div></div>`);
  root.appendChild(lateCard);

  const diag = result.diagnostics;
  const diagCard = el(`<div class="card"><div class="card-head"><div><h2>Ingestion diagnostics</h2><p>What the engine saw, before any population filter.</p></div></div>
    <div class="table-wrap"><table class="tbl-small"><thead><tr><th>Measure</th><th class="num">Value</th><th>Measure</th><th class="num">Value</th></tr></thead><tbody>
    ${pairRows([
      ["Assignments total", diag.assignments_total], ["Assignments in window", diag.assignments_in_window],
      ["Events total", diag.events_total], ["Events in window", diag.events_in_window],
      ["Outside-window events", diag.events_outside_window], ["Orphan events", diag.orphan_events],
      ["Duplicate payloads", diag.duplicate_events], ["Before assignment", diag.pre_assignment_events],
      ["Stale metric events", diag.stale_metric_events], ["Units", diag.units],
    ])}
    </tbody></table></div></div>`);
  root.appendChild(diagCard);
}

function pairRows(pairs) {
  let html = "";
  for (let i = 0; i < pairs.length; i += 2) {
    const a = pairs[i];
    const b = pairs[i + 1] || ["", ""];
    html += `<tr><td>${esc(a[0])}</td><td class="num">${fmt.n(a[1])}</td><td>${esc(b[0])}</td><td class="num">${fmt.n(b[1])}</td></tr>`;
  }
  return html;
}

// ---------------------------------------------------------------- dialogs
const dialog = () => document.getElementById("dialog");

function openNewExperiment() {
  const d = dialog();
  const form = document.getElementById("dialog-form");
  form.innerHTML = `
    <h2>New experiment</h2>
    <div class="form-grid">
      <label class="form-field">ID<input name="id" required placeholder="checkout_v2"></label>
      <label class="form-field">Name<input name="name" required placeholder="Checkout redesign"></label>
      <label class="form-field">Allocation
        <select name="allocation_method"><option value="fixed">Fixed split</option><option value="stratified">Stratified random</option></select>
      </label>
      <label class="form-field">Strata columns (comma separated)<input name="strata_columns" placeholder="country,platform"></label>
      <label class="form-field">Treatment share<input name="treatment_share" type="number" min="0" max="1" step="0.01" value="0.5"></label>
      <label class="form-field">Sample window start<input name="effective_from" placeholder="2026-10-01T00:00:00Z" value="2026-10-01T00:00:00Z"></label>
    </div>
    <div class="form-grid">
      <label class="form-field check"><input type="checkbox" name="cross_device" checked> Cross-device users</label>
      <label class="form-field check"><input type="checkbox" name="allow_switch" checked> Switching allowed</label>
      <label class="form-field check"><input type="checkbox" name="demo" checked> Generate demo data (800 units)</label>
    </div>
    <label class="form-field">Metrics (JSON)
      <textarea name="metrics">[
  {"key": "exposure", "name": "Exposure", "type": "proportion"},
  {"key": "purchase", "name": "Purchase", "type": "proportion"},
  {"key": "revenue", "name": "Revenue", "type": "continuous", "aggregation": "sum"}
]</textarea>
    </label>
    <div class="dialog-actions">
      <button class="btn ghost" type="button" data-close>Cancel</button>
      <button class="btn" type="submit">Create</button>
    </div>`;
  form.querySelector("[data-close]").addEventListener("click", () => d.close());
  form.onsubmit = async (e) => {
    e.preventDefault();
    const fd = new FormData(form);
    try {
      const metrics = JSON.parse(fd.get("metrics"));
      const allocation = fd.get("allocation_method");
      const payload = {
        id: fd.get("id"),
        name: fd.get("name"),
        allocation_method: allocation,
        strata_columns: allocation === "stratified"
          ? String(fd.get("strata_columns") || "").split(",").map((s) => s.trim()).filter(Boolean)
          : [],
        cross_device: fd.get("cross_device") === "on",
        allow_switch: fd.get("allow_switch") === "on",
        metrics,
        effective_from: fd.get("effective_from"),
        config: { fixed: { treatment_share: Number(fd.get("treatment_share")) } },
      };
      const detail = await post("/api/experiments", payload);
      if (fd.get("demo") === "on") {
        await post(`/api/experiments/${encodeURIComponent(payload.id)}/simulate`, { n_units: 800, seed: 7 });
      }
      d.close();
      state.expId = payload.id;
      await loadExperiments();
    } catch (err) {
      alert(err.message);
    }
  };
  d.showModal();
}

function openVersionDialog(kind) {
  const d = dialog();
  const form = document.getElementById("dialog-form");
  const versions = state.detail.versions;
  const titles = {
    pause: "Pause experiment",
    resume: "Resume experiment",
    metric_change: "Change metric definition",
    rollback: "Roll back to an earlier version",
    config_change: "Configuration change",
  };
  const metricKeys = state.detail.experiment.metrics.map((m) => m.key).filter((k) => k !== "exposure");
  form.innerHTML = `
    <h2>${titles[kind]}</h2>
    <p class="small secondary">${
      kind === "pause"
        ? "Freezes the sample at the transition instant. Late results after this point are flagged, not folded in."
        : kind === "rollback"
          ? "Replays the target version's frozen window with its rules, so the revert is reproducible and comparable."
          : kind === "metric_change"
            ? "Opens a new version, bumping the metric definition. Events recorded under the old definition are excluded with a reason."
            : kind === "resume"
              ? "Starts a fresh sample window from now; earlier data stays frozen in the paused version."
              : "Applies a config change and re-evaluates the running sample."
    }</p>
    <div class="form-grid">
      <label class="form-field">Effective from<input name="effective_from" placeholder="now" value="${new Date().toISOString().replace(/\.\d+Z$/, "Z")}"></label>
      ${kind === "rollback"
        ? `<label class="form-field">Roll back to<select name="rollback_to">${versions.slice().reverse().map((v) => `<option value="${v.number}">v${v.number} · ${esc(v.label)}</option>`).join("")}</select></label>`
        : ""}
      ${kind === "metric_change"
        ? `<label class="form-field">Changed metrics (comma separated)<input name="changed_metrics" placeholder="${esc(metricKeys.join(","))}"></label>`
        : ""}
      <label class="form-field">Reason<input name="reason" placeholder="Why this version exists"></label>
    </div>
    ${kind === "config_change" ? `<label class="form-field">Config patch (JSON)<textarea name="config">{"rules": {}}</textarea></label>` : ""}
    <div class="dialog-actions">
      <button class="btn ghost" type="button" data-close>Cancel</button>
      <button class="btn" type="submit">${kind === "pause" ? "Pause" : kind === "rollback" ? "Roll back" : "Open version"}</button>
    </div>`;
  form.querySelector("[data-close]").addEventListener("click", () => d.close());
  form.onsubmit = async (e) => {
    e.preventDefault();
    const fd = new FormData(form);
    const payload = {
      kind,
      reason: fd.get("reason") || "",
      effective_from: fd.get("effective_from") || undefined,
    };
    if (kind === "rollback") payload.rollback_to = Number(fd.get("rollback_to"));
    if (kind === "metric_change") {
      const keys = String(fd.get("changed_metrics") || "")
        .split(",").map((s) => s.trim()).filter(Boolean);
      payload.config = { rules: { changed_metrics: keys } };
    }
    if (kind === "config_change") {
      try {
        payload.config = JSON.parse(fd.get("config") || "{}");
      } catch (err) {
        alert(`Config JSON: ${err.message}`);
        return;
      }
    }
    try {
      const detail = await post(`/api/experiments/${encodeURIComponent(state.expId)}/versions`, payload);
      d.close();
      state.versionId = detail.versions.at(-1).id;
      await loadDetail({ keepVersion: true });
    } catch (err) {
      alert(err.message);
    }
  };
  d.showModal();
}

// ---------------------------------------------------------------- theme
function applyTheme(theme) {
  if (theme === "dark" || theme === "light") {
    document.documentElement.dataset.theme = theme;
  } else {
    delete document.documentElement.dataset.theme;
  }
}
function toggleTheme() {
  const current = document.documentElement.dataset.theme
    || (window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
  const next = current === "dark" ? "light" : "dark";
  localStorage.setItem("exp-theme", next);
  applyTheme(next);
}

boot().catch((err) => {
  document.getElementById("view").innerHTML = `<div class="card callout fail">${esc(err.message)}</div>`;
});
