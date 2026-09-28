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

function splitPnl(label, dollars, frac, fine) {
  const node = metric(label, "", tone(dollars));
  const value = node.querySelector(".v");
  value.replaceChildren(
    pair(formatUsd(dollars, { signed: true }), formatPct(frac, { signed: true, digits: 2 }))
  );
  node.append(el("p", "fine", fine));
  return node;
}

function renderSleeve(derived, { hero = false } = {}) {
  const card = el("article", `sleeve ${derived.sleeve}${hero ? " hero" : ""}`);
  const head = el("header", "sleeve-head");
  head.append(el("h2", null, derived.label));
  if (derived.asOf) head.append(el("time", null, String(derived.asOf)));
  card.append(head);

  const grid = el("div", "metrics");
  const balance = metric(
    "Running balance (book)",
    formatUsd(derived.runningBalance),
    tone(derived.runningBalance)
  );
  balance.append(el("p", "fine", "book = start + realized + unrealized"));
  grid.append(
    splitPnl("Realized P&L", derived.realizedPnl, derived.realizedPnlFrac, "closed exits"),
    splitPnl("Unrealized P&L", derived.unrealizedPnl, derived.unrealizedPnlFrac, "open MTM"),
    balance,
    metric("Start", formatUsd(derived.seed))
  );

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
  const caption = el("caption", null, `${labelFor(sleeve)} fills. Trade P&L dollars are the book seed times pnl_frac.`);
  const thead = el("thead");
  const headRow = el("tr");
  for (const label of ["Time", "Ticker", "Side", "Qty", "Trade P&L", "Running P&L", "Running balance (book)", "Why"]) {
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

function oosRows(payload) {
  if (Array.isArray(payload)) return payload.filter((row) => row && typeof row === "object");
  if (payload && Array.isArray(payload.rows)) return payload.rows.filter((row) => row && typeof row === "object");
  return [];
}

function sameSleeve(row, sleeve) {
  const value = String(row.sleeve || row.book || row.asset_class || "").trim().toLowerCase();
  if (sleeve === "equities") return value === "equities" || value === "equity";
  return value === sleeve;
}

function formatMetricRow(row) {
  const hasFit = row.auc != null || row.brier != null || row.n_long != null || row.after_cost_mean != null;
  if (!hasFit) return summarizeOosRow(row);
  const bits = [String(row.model || row.name || "model")];
  if (row.auc != null) bits.push(`AUC ${row.auc}`);
  if (row.brier != null) bits.push(`Brier ${row.brier}`);
  if (row.n_long != null) bits.push(`n_long ${row.n_long}`);
  if (row.after_cost_mean != null) bits.push(`after cost ${row.after_cost_mean}`);
  if (row.sleeve_ir_vs_spy != null) bits.push(`IR vs SPY ${row.sleeve_ir_vs_spy}`);
  if (row.sleeve_sharpe != null) bits.push(`Sharpe ${row.sleeve_sharpe}`);
  if (row.n_cohorts != null) bits.push(`${row.n_cohorts} cohorts`);
  if (row.promoted === false) bits.push("not promoted");
  if (row.promoted === true) bits.push("promoted");
  if (row.note) bits.push(String(row.note));
  return bits.join(" \u00b7 ");
}

function summarizeOosRow(row) {
  if (row.hit_rate != null || row.avg_return != null || row.n != null || row.window) return oosText(row);
  const skip = new Set(["sleeve", "book", "name"]);
  const bits = [];
  for (const [key, value] of Object.entries(row)) {
    if (skip.has(key) || value == null || value === "") continue;
    bits.push(`${key} ${value}`);
    if (bits.length >= 4) break;
  }
  return bits.join(" \u00b7 ") || "Pending T04.";
}

function renderModels(payload, oosPayload) {
  modelsEl.replaceChildren();
  const models = payload && Array.isArray(payload.models) ? payload.models.slice() : [];
  const exported = oosRows(oosPayload);
  if (payload && payload.note) modelsEl.append(el("p", "note", String(payload.note)));
  if (oosPayload && oosPayload.updated_at) {
    modelsEl.append(el("p", "fine", `OOS updated ${oosPayload.updated_at}`));
  }
  if (!models.length && exported.length) {
    for (const row of exported) {
      models.push({
        sleeve: row.sleeve || row.book || (String(row.asset_class || "").toLowerCase() === "equity" ? "equities" : row.asset_class) || "",
        name: row.name || row.model || row.sleeve || "Model",
        used: row.used || "\u2014",
        training: row.training || "\u2014",
        data_source: row.data_source || row.data || "\u2014",
        oos: row,
      });
    }
  }
  if (!models.length) {
    modelsEl.append(el("p", "empty", "No model cards in this snapshot."));
    return;
  }
  const grid = el("div", "model-grid");
  const used = new Set();
  for (const model of models) {
    const mine = exported.filter((row) => sameSleeve(row, String(model.sleeve || "").toLowerCase()));
    for (const row of mine) used.add(row);
    const card = el("article", "sleeve model-card");
    const head = el("header", "sleeve-head");
    head.append(el("h2", null, model.name || "Model"));
    if (model.sleeve) head.append(el("p", "fine", String(model.sleeve)));
    card.append(head);
    const list = el("dl");
    const fields = [
      ["Used", model.used],
      ["Training", model.training],
      ["Data", model.data_source],
    ];
    for (const [label, value] of fields) {
      list.append(el("dt", null, label), el("dd", null, value == null || value === "" ? "\u2014" : String(value)));
    }
    list.append(el("dt", null, "OOS"));
    const oosDd = el("dd");
    if (mine.length) {
      const items = el("ul", "oos-list");
      for (const row of mine) items.append(el("li", null, formatMetricRow(row)));
      oosDd.append(items);
    } else {
      oosDd.textContent = oosText(model.oos);
    }
    list.append(oosDd);
    card.append(list);
    grid.append(card);
  }
  modelsEl.append(grid);
  const rest = exported.filter((row) => !used.has(row));
  if (rest.length) {
    const extra = el("p", "note", rest.map(summarizeOosRow).join("; "));
    modelsEl.append(extra);
  }
}

async function main() {
  tabSleeves.addEventListener("click", () => showTab("sleeves"));
  tabModels.addEventListener("click", () => showTab("models"));
  try {
    const [summary, trades, meta, models, oos] = await Promise.all([
      loadJson("data/kpi_summary.json"),
      loadJson("data/kpi_trades_scrubbed.json"),
      loadJson("data/meta.json"),
      loadJson("data/models.json").catch(() => ({ models: [], note: "data/models.json is not in this snapshot." })),
      loadJson("data/models_oos.json").catch(() => ({ rows: [] })),
    ]);
    render(summary, trades, meta);
    renderModels(models, oos);
  } catch (error) {
    statusEl.replaceChildren(el("p", "status-copy", "KPI JSON did not load."));
    boardEl.replaceChildren(el("p", "empty", String(error.message || error)));
  }
}

main();
