import {
  deriveSleeve,
  formatPct,
  formatUsd,
  headroomFill,
  labelFor,
  sleeveKey,
  sortSleeves,
  tone,
} from "./derive.js";

const statusEl = document.querySelector("#status");
const boardEl = document.querySelector("#board");
const tapesEl = document.querySelector("#tapes");
const modelsEl = document.querySelector("#models");
const panelSleeves = document.querySelector("#panel-sleeves");
const panelModels = document.querySelector("#panel-models");
const tabSleeves = document.querySelector("#tab-sleeves");
const tabModels = document.querySelector("#tab-models");

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
}

function metric(label, value, valueClass) {
  const wrap = el("div", "metric");
  wrap.append(el("div", "k", label), el("div", `v ${valueClass || ""}`.trim(), value));
  return wrap;
}

function pair(primary, secondary) {
  const wrap = el("div", "pair");
  wrap.append(el("span", "primary", primary));
  if (secondary) wrap.append(el("span", "secondary", secondary));
  return wrap;
}

async function loadJson(path) {
  const response = await fetch(path, { cache: "no-cache" });
  if (!response.ok) throw new Error(`${path} ${response.status}`);
  return response.json();
}

function renderStatus(meta) {
  statusEl.replaceChildren();
  const source = meta?.source === "supabase" ? "Supabase export" : "Sample snapshot";
  const chip = el("span", meta?.source === "supabase" ? "chip live" : "chip sample", source);
  statusEl.append(chip);
  const when = meta?.fetched_at || meta?.note || "";
  if (when) statusEl.append(el("p", "status-copy", String(when)));
}

function renderSleeve(derived, { hero = false } = {}) {
  const card = el("article", `sleeve ${derived.sleeve}${hero ? " hero" : ""}`);
  const head = el("header", "sleeve-head");
  head.append(el("h2", null, derived.label));
  if (derived.asOf) head.append(el("time", null, String(derived.asOf)));
  card.append(head);

  const grid = el("div", "metrics");
  grid.append(
    metric("Start", formatUsd(derived.seed)),
    metric("Running balance", formatUsd(derived.runningBalance), tone(derived.runningBalance)),
    metric(
      "P&L",
      "",
      tone(derived.runningPnl)
    )
  );
  const pnl = grid.lastChild.querySelector(".v");
  pnl.replaceChildren(pair(formatUsd(derived.runningPnl, { signed: true }), formatPct(derived.runningPnlFrac, { signed: true })));
  pnl.classList.add(tone(derived.runningPnl));

  grid.append(metric("Day P&L", formatUsd(derived.dayPnl, { signed: true }), tone(derived.dayPnl)));

  const kill = metric("Day kill rail");
  kill.querySelector(".v").replaceChildren(
    pair(formatPct(derived.dayKillFrac), derived.dayKill == null ? "" : formatUsd(derived.dayKill))
  );
  grid.append(kill);

  const headroom = metric("Kill headroom");
  const headroomValue = headroom.querySelector(".v");
  headroomValue.replaceChildren(
    pair(
      derived.killHeadroomFrac == null ? "—" : `${formatPct(derived.killHeadroomFrac)} of book`,
      formatUsd(derived.killHeadroom)
    )
  );
  const fill = headroomFill(derived);
  if (fill != null) {
    const meter = el("div", "meter");
    meter.setAttribute("role", "img");
    meter.setAttribute(
      "aria-label",
      `Kill headroom ${formatPct(derived.killHeadroomFrac)} of book against a ${formatPct(derived.dayKillFrac)} rail`
    );
    const bar = el("span");
    bar.style.width = `${fill}%`;
    if (fill < 25) bar.className = "thin";
    meter.append(bar);
    headroom.append(meter);
  }
  grid.append(headroom);

  if (derived.dayTargetFrac != null || derived.sleeve === "crypto") {
    const target = metric("Day target");
    const bits = [formatPct(derived.dayTargetFrac, { signed: true })];
    if (derived.dayTarget != null) bits.push(formatUsd(derived.dayTarget, { signed: true }));
    target.querySelector(".v").replaceChildren(pair(bits[0], bits[1] || ""));
    if (derived.sleeve === "crypto") target.append(el("p", "fine", "Realized only"));
    grid.append(target);
  }

  card.append(grid);
  if (derived.note) card.append(el("p", "note", String(derived.note)));
  return card;
}

function renderTape(sleeve, trades) {
  const seedRow = { sleeve };
  const section = el("section", `tape ${sleeve}`);
  section.append(el("h3", null, labelFor(sleeve)));
  if (!trades.length) {
    section.append(el("p", "empty", "No scrubbed fills."));
    return section;
  }
  const wrap = el("div", "table-wrap");
  const table = el("table");
  const caption = el("caption", null, `${labelFor(sleeve)} fills. Trade P&L dollars are the sleeve seed times pnl_frac.`);
  const thead = el("thead");
  const headRow = el("tr");
  for (const label of ["Time", "Ticker", "Side", "Qty", "Trade P&L", "Running P&L", "Running balance", "Why"]) {
    headRow.append(el("th", null, label));
  }
  thead.append(headRow);
  const tbody = el("tbody");
  const derivedSeed = deriveSleeve(seedRow).seed;
  for (const trade of trades) {
    const shaped = deriveSleeve({ ...trade, sleeve });
    const seed = shaped.seed || derivedSeed;
    const pnl = seed == null || trade.pnl_frac == null ? null : Math.round((seed * Number(trade.pnl_frac) + Number.EPSILON) * 100) / 100;
    const tr = el("tr");
    const side = el("td");
    const pill = el("span", `pill ${trade.side || ""}`, trade.side || "—");
    side.append(pill);
    const cells = [
      el("td", null, String(trade.ts || "—")),
      el("td", "ticker", trade.ticker || "—"),
      side,
      el("td", "num", trade.qty == null ? "—" : String(trade.qty)),
      el("td", `num ${tone(pnl)}`, formatUsd(pnl, { signed: true })),
      el("td", `num ${tone(shaped.runningPnl)}`, formatUsd(shaped.runningPnl, { signed: true })),
      el("td", `num ${tone(shaped.runningBalance)}`, formatUsd(shaped.runningBalance)),
      el("td", "why", trade.why || ""),
    ];
    tr.append(...cells);
    tbody.append(tr);
  }
  table.append(caption, thead, tbody);
  wrap.append(table);
  section.append(wrap);
  return section;
}

function render(summaryRows, tradeRows, meta) {
  renderStatus(meta || {});
  const sleeves = sortSleeves(Array.isArray(summaryRows) ? summaryRows : [], undefined);
  boardEl.replaceChildren();
  const combined = sleeves.find((row) => row.sleeve === "combined");
  const rest = sleeves.filter((row) => row.sleeve !== "combined");
  if (combined) boardEl.append(renderSleeve(combined, { hero: true }));
  const grid = el("div", "sleeve-grid");
  for (const row of rest) grid.append(renderSleeve(row));
  if (!combined && !rest.length) {
    boardEl.append(el("p", "empty", "kpi_summary has no sleeve rows."));
  } else {
    boardEl.append(grid);
  }

  const grouped = new Map();
  for (const trade of Array.isArray(tradeRows) ? tradeRows : []) {
    const key = sleeveKey(trade);
    if (!grouped.has(key)) grouped.set(key, []);
    grouped.get(key).push(trade);
  }
  tapesEl.replaceChildren();
  for (const key of ["crypto", "equities"]) {
    const rows = (grouped.get(key) || []).slice().sort((a, b) => String(b.ts || "").localeCompare(String(a.ts || "")));
    tapesEl.append(renderTape(key, rows));
  }
}

function showTab(name) {
  const models = name === "models";
  panelSleeves.hidden = models;
  panelModels.hidden = !models;
  tabSleeves.setAttribute("aria-selected", models ? "false" : "true");
  tabModels.setAttribute("aria-selected", models ? "true" : "false");
}

function modelRows(payload) {
  if (Array.isArray(payload)) return payload;
  if (payload && Array.isArray(payload.rows)) return payload.rows;
  return [];
}

function renderModels(payload) {
  modelsEl.replaceChildren();
  const rows = modelRows(payload).filter((row) => row && typeof row === "object");
  const note = payload && !Array.isArray(payload) ? payload.note : "";
  if (note) modelsEl.append(el("p", "note", String(note)));
  if (!rows.length) {
    modelsEl.append(el("p", "empty", "No out-of-sample model rows in this snapshot."));
    return;
  }
  const keys = [];
  for (const row of rows) {
    for (const key of Object.keys(row)) {
      if (!keys.includes(key)) keys.push(key);
    }
  }
  const wrap = el("div", "table-wrap");
  const table = el("table");
  table.append(el("caption", null, "models_oos snapshot. Values are shown as exported."));
  const head = el("tr");
  for (const key of keys) head.append(el("th", null, key));
  const thead = el("thead");
  thead.append(head);
  const tbody = el("tbody");
  for (const row of rows) {
    const tr = el("tr");
    for (const key of keys) {
      const value = row[key];
      tr.append(el("td", null, value == null ? "—" : String(value)));
    }
    tbody.append(tr);
  }
  table.append(thead, tbody);
  wrap.append(table);
  modelsEl.append(wrap);
}

async function main() {
  tabSleeves.addEventListener("click", () => showTab("sleeves"));
  tabModels.addEventListener("click", () => showTab("models"));
  try {
    const [summary, trades, meta, models] = await Promise.all([
      loadJson("data/kpi_summary.json"),
      loadJson("data/kpi_trades_scrubbed.json"),
      loadJson("data/meta.json"),
      loadJson("data/models_oos.json").catch(() => ({ rows: [], note: "models_oos.json is not in this snapshot." })),
    ]);
    render(summary, trades, meta);
    renderModels(models);
  } catch (error) {
    statusEl.replaceChildren(el("p", "status-copy", "KPI JSON did not load."));
    boardEl.replaceChildren(el("p", "empty", String(error.message || error)));
  }
}

main();
