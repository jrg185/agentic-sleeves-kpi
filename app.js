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
      derived.killHeadroomFrac == null ? "\u2014" : `${formatPct(derived.killHeadroomFrac)} of book`,
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
    const pill = el("span", `pill ${trade.side || ""}`, trade.side || "\u2014");
    side.append(pill);
    const cells = [
      el("td", null, String(trade.ts || "\u2014")),
      el("td", "ticker", trade.ticker || "\u2014"),
      side,
      el("td", "num", trade.qty == null ? "\u2014" : String(trade.qty)),
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

function oosText(oos) {
  if (!oos || typeof oos !== "object") return "Pending T04.";
  const pending = oos.status === "placeholder" || (oos.hit_rate == null && oos.avg_return == null && oos.n == null);
  if (pending) return oos.note || "Pending T04.";
  const bits = [];
  if (oos.window) bits.push(String(oos.window));
  if (oos.hit_rate != null) bits.push(`hit ${oos.hit_rate}`);
  if (oos.avg_return != null) bits.push(`avg ${oos.avg_return}`);
  if (oos.n != null) bits.push(`n=${oos.n}`);
  return bits.join(" \u00b7 ") || "Pending T04.";
}

function renderModels(payload) {
  modelsEl.replaceChildren();
  const models = payload && Array.isArray(payload.models) ? payload.models : [];
  if (payload && payload.note) modelsEl.append(el("p", "note", String(payload.note)));
  if (!models.length) {
    modelsEl.append(el("p", "empty", "No model cards in data/models.json."));
    return;
  }
  const grid = el("div", "model-grid");
  for (const model of models) {
    const card = el("article", "sleeve model-card");
    const head = el("header", "sleeve-head");
    head.append(el("h2", null, model.name || "Model"));
    if (model.sleeve) head.append(el("p", "fine", String(model.sleeve)));
    card.append(head);
    const list = el("dl");
    const rows = [
      ["Used", model.used],
      ["Training", model.training],
      ["Data", model.data_source],
      ["OOS", oosText(model.oos)],
    ];
    for (const [label, value] of rows) {
      list.append(el("dt", null, label), el("dd", null, value == null || value === "" ? "\u2014" : String(value)));
    }
    card.append(list);
    grid.append(card);
  }
  modelsEl.append(grid);
}

async function main() {
  tabSleeves.addEventListener("click", () => showTab("sleeves"));
  tabModels.addEventListener("click", () => showTab("models"));
  try {
    const [summary, trades, meta, models] = await Promise.all([
      loadJson("data/kpi_summary.json"),
      loadJson("data/kpi_trades_scrubbed.json"),
      loadJson("data/meta.json"),
      loadJson("data/models.json").catch(() => ({ models: [], note: "data/models.json is not in this snapshot." })),
    ]);
    render(summary, trades, meta);
    renderModels(models);
  } catch (error) {
    statusEl.replaceChildren(el("p", "status-copy", "KPI JSON did not load."));
    boardEl.replaceChildren(el("p", "empty", String(error.message || error)));
  }
}

main();
