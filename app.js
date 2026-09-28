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
import { csvFilename, linkSegments, preferredWhy, tapeCsv, tapeOpenKey } from "./tape.js";

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

const STALE_AFTER_MS = 3 * 60 * 60 * 1000;
// Sleeve as_of is the warehouse MTM clock. Export can rewrite JSON without moving it.
const SNAPSHOT_STALE_MS = 60 * 60 * 1000;

function parseTime(value) {
  if (!value) return null;
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? null : date;
}

function formatEt(value) {
  const date = parseTime(value);
  if (!date) return value ? String(value) : "";
  return new Intl.DateTimeFormat("en-US", {
    timeZone: "America/New_York",
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
    timeZoneName: "short",
  }).format(date);
}

function latestStamp(values) {
  let best = null;
  let bestMs = -Infinity;
  for (const value of values) {
    const date = parseTime(value);
    if (date && date.getTime() >= bestMs) {
      best = value;
      bestMs = date.getTime();
    }
  }
  return best;
}

async function loadJson(path, token) {
  const bust = encodeURIComponent(String(token || Date.now()));
  const response = await fetch(`${path}?t=${bust}`, { cache: "no-store" });
  if (!response.ok) throw new Error(`${path} ${response.status}`);
  return response.json();
}

function agePhrase(ms) {
  const minutes = Math.max(0, Math.round(ms / 60000));
  if (minutes < 60) return `${minutes} minutes`;
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  if (!rest) return hours === 1 ? "1 hour" : `${hours} hours`;
  return `${hours}h ${rest}m`;
}

function renderStatus(meta, sleeveAsOf) {
  statusEl.replaceChildren();
  const exportStatus = String(meta?.export_status || "").toLowerCase();
  const refreshedAt = parseTime(meta?.fetched_at);
  const snapshotAt = parseTime(sleeveAsOf);
  const agedOut = refreshedAt != null && Date.now() - refreshedAt.getTime() > STALE_AFTER_MS;
  const snapshotAge = snapshotAt == null ? null : Date.now() - snapshotAt.getTime();
  const warehouse = String(meta?.warehouse_status || "").toLowerCase();
  const mtmStale = meta?.source === "supabase" && snapshotAge != null && snapshotAge > SNAPSHOT_STALE_MS;
  const exportBroken = exportStatus === "error" || exportStatus === "stale" || agedOut;
  const frozenLabel = formatEt(meta?.snapshot_as_of || sleeveAsOf);
  let chipClass = "chip sample";
  let chipText = "Sample snapshot";
  if (warehouse === "read-only") {
    chipClass = "chip error";
    chipText = "Warehouse read-only";
  } else if (warehouse === "disk-full") {
    chipClass = "chip error";
    chipText = "Warehouse disk full";
  } else if (exportStatus === "error") {
    chipClass = "chip error";
    chipText = "Export failed";
  } else if (exportStatus === "stale" || agedOut) {
    chipClass = "chip stale";
    chipText = "Stale snapshot";
  } else if (mtmStale) {
    chipClass = "chip stale";
    chipText = "MTM stale";
  } else if (meta?.source === "supabase") {
    chipClass = "chip live";
    chipText = "Supabase export";
  }
  const chip = el("span", chipClass, chipText);
  statusEl.append(chip);

  const lines = el("div", "status-lines");
  const refreshed = formatEt(meta?.fetched_at);
  const headline = el(
    "p",
    "status-copy",
    refreshed ? `Last refreshed ${refreshed}` : "Last refreshed time is missing from this snapshot."
  );
  lines.append(headline);
  const asOf = formatEt(sleeveAsOf);
  if (asOf) lines.append(el("p", "status-copy", `Sleeve as of ${asOf}`));
  if (warehouse === "read-only") {
    lines.append(
      el(
        "p",
        "status-copy status-error",
        frozenLabel
          ? `warehouse read-only — snapshot frozen at ${frozenLabel}`
          : "warehouse read-only — snapshot frozen"
      )
    );
  } else if (warehouse === "disk-full") {
    lines.append(
      el(
        "p",
        "status-copy status-error",
        frozenLabel
          ? `warehouse disk full — snapshot frozen at ${frozenLabel}`
          : "warehouse disk full — snapshot frozen"
      )
    );
  } else if (meta?.export_error) {
    lines.append(el("p", "status-copy status-error", String(meta.export_error)));
  } else if (exportBroken) {
    lines.append(el("p", "status-copy status-error", "This JSON is not a fresh warehouse export."));
  } else if (mtmStale) {
    lines.append(
      el(
        "p",
        "status-copy status-warn",
        `Warehouse MTM is ${agePhrase(snapshotAge)} old. Export only re-reads kpi_summary. This repo does not refresh kpi_sleeve_snapshots.`
      )
    );
  } else if (meta?.source === "supabase" && !asOf) {
    lines.append(
      el(
        "p",
        "status-copy status-warn",
        "Sleeve as_of is missing, so this page cannot tell whether warehouse MTM moved."
      )
    );
  } else if (!refreshed && meta?.note) {
    lines.append(el("p", "status-copy", String(meta.note)));
  }
  const attempted = formatEt(meta?.export_attempted_at);
  if (attempted && attempted !== refreshed && (exportStatus === "error" || exportStatus === "stale")) {
    lines.append(el("p", "status-copy", `Last export attempt ${attempted}`));
  }
  statusEl.append(lines);
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
  if (derived.asOf) {
    const when = el("time", null, formatEt(derived.asOf));
    when.dateTime = String(derived.asOf);
    head.append(when);
  }
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

  const target = metric("Day target");
  const bits = [formatPct(derived.dayTargetFrac, { signed: true })];
  if (derived.dayTarget != null) bits.push(formatUsd(derived.dayTarget, { signed: true }));
  target.querySelector(".v").replaceChildren(pair(bits[0], bits[1] || ""));
  if (derived.sleeve === "crypto") target.append(el("p", "fine", "Realized only"));
  grid.append(target);

  card.append(grid);
  if (derived.note) card.append(el("p", "note", String(derived.note)));
  return card;
}

function readFrac(row, key) {
  if (!row || row[key] == null || row[key] === "") return null;
  const n = Number(row[key]);
  return Number.isFinite(n) ? n : null;
}

function seedTimes(seed, frac) {
  if (seed == null || frac == null) return null;
  return Math.round((seed * frac + Number.EPSILON) * 100) / 100;
}

function notionalText(seed, frac) {
  if (frac == null) return "\u2014";
  const dollars = seedTimes(seed, frac);
  const pct = formatPct(frac, { digits: 2 });
  return dollars == null ? pct : `${formatUsd(dollars)} (${pct})`;
}

function tradeStamp(trade) {
  return trade?.timestamp_et || trade?.ts || "";
}

function storedTapeOpen(sleeve) {
  try {
    const value = localStorage.getItem(tapeOpenKey(sleeve));
    if (value === "closed") return false;
    if (value === "open") return true;
  } catch {
    /* localStorage can throw in private mode. Default to open. */
  }
  return true;
}

function storeTapeOpen(sleeve, open) {
  try {
    localStorage.setItem(tapeOpenKey(sleeve), open ? "open" : "closed");
  } catch {
    /* Ignore quota and private-mode failures. The toggle still works this visit. */
  }
}

function fillLinked(parent, text, sleeve) {
  for (const part of linkSegments(text, sleeve)) {
    if (part.type === "link") {
      const anchor = document.createElement("a");
      anchor.href = part.href;
      anchor.target = "_blank";
      anchor.rel = "noopener";
      anchor.textContent = part.text;
      parent.append(anchor);
    } else if (part.text) {
      parent.append(document.createTextNode(part.text));
    }
  }
}

function tapeFields(sleeve, trade) {
  const shaped = deriveSleeve({ ...trade, sleeve });
  const seed = shaped.seed || deriveSleeve({ sleeve }).seed;
  const pnlFrac = readFrac(trade, "pnl_frac_of_book") ?? readFrac(trade, "pnl_frac");
  const pnl = seedTimes(seed, pnlFrac);
  const why = preferredWhy(trade);
  return {
    time: formatEt(tradeStamp(trade)) || "\u2014",
    ticker: trade.ticker || "\u2014",
    side: trade.side || "\u2014",
    notional: notionalText(seed, readFrac(trade, "notional_frac_of_book")),
    tradePnl: formatUsd(pnl, { signed: true }),
    runningPnl: formatUsd(shaped.runningPnl, { signed: true }),
    runningBalance: formatUsd(shaped.runningBalance),
    pnl,
    runningPnlValue: shaped.runningPnl,
    runningBalanceValue: shaped.runningBalance,
    why: why.text,
    whyKind: why.kind,
    whyUuid: why.uuid,
  };
}

function renderWhyCell(fields, sleeve) {
  const cell = el("td", "why");
  if (fields.whyKind === "machine") {
    const stack = el("div", "why-stack");
    stack.append(el("span", "why-label", fields.why));
    const details = document.createElement("details");
    details.className = "why-id";
    const summary = document.createElement("summary");
    summary.textContent = "Order id";
    details.append(summary);
    if (fields.whyUuid) details.append(el("span", "why-uuid", fields.whyUuid));
    stack.append(details);
    cell.append(stack);
    cell.title = fields.why;
    return cell;
  }
  if (fields.why) {
    fillLinked(cell, fields.why, sleeve);
    cell.title = fields.why;
  }
  return cell;
}

function downloadCsv(filename, text) {
  const blob = new Blob([text], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  link.rel = "noopener";
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1500);
}

function renderTape(sleeve, trades) {
  const section = el("section", `tape ${sleeve}`);
  const head = el("div", "tape-head");
  const heading = el("h3");
  const toggle = document.createElement("button");
  toggle.type = "button";
  toggle.className = "tape-toggle";
  const panelId = `tape-panel-${sleeve}`;
  toggle.setAttribute("aria-controls", panelId);
  const open = storedTapeOpen(sleeve);
  toggle.setAttribute("aria-expanded", open ? "true" : "false");
  toggle.append(document.createTextNode(labelFor(sleeve)));
  const chevron = el("span", "chevron");
  chevron.setAttribute("aria-hidden", "true");
  toggle.append(chevron);
  heading.append(toggle);

  const exportBtn = document.createElement("button");
  exportBtn.type = "button";
  exportBtn.className = "tape-export";
  exportBtn.textContent = "Download CSV";
  exportBtn.setAttribute("aria-label", `Download ${labelFor(sleeve)} CSV`);

  const panel = el("div", "tape-panel");
  panel.id = panelId;
  panel.hidden = !open;
  toggle.addEventListener("click", () => {
    const next = toggle.getAttribute("aria-expanded") !== "true";
    toggle.setAttribute("aria-expanded", next ? "true" : "false");
    panel.hidden = !next;
    storeTapeOpen(sleeve, next);
  });
  exportBtn.addEventListener("click", () => {
    const rows = trades.map((trade) => tapeFields(sleeve, trade));
    downloadCsv(csvFilename(sleeve), tapeCsv(rows));
  });

  head.append(heading, exportBtn);
  section.append(head);

  if (!trades.length) {
    panel.append(el("p", "empty", "No scrubbed fills."));
    section.append(panel);
    return section;
  }

  const wrap = el("div", "table-wrap");
  const table = el("table");
  const captionText = `${labelFor(sleeve)} fills. Trade P&L, running P&L, and running balance are the book seed times the fraction. Running balance is start plus realized P&L through that fill.`;
  const caption = el("caption", "sr-only", `${labelFor(sleeve)} fills`);
  const note = el("p", "tape-note", captionText);
  note.id = `tape-note-${sleeve}`;
  table.setAttribute("aria-describedby", note.id);
  const thead = el("thead");
  const headRow = el("tr");
  for (const label of ["Time", "Ticker", "Side", "Notional", "Trade P&L", "Running P&L", "Running balance", "Why"]) {
    const th = el("th", null, label);
    th.scope = "col";
    headRow.append(th);
  }
  thead.append(headRow);
  const tbody = el("tbody");
  for (const trade of trades) {
    const fields = tapeFields(sleeve, trade);
    const tr = el("tr");
    const side = el("td");
    const pill = el("span", `pill ${trade.side || ""}`, fields.side);
    side.append(pill);
    tr.append(
      el("td", null, fields.time),
      el("td", "ticker", fields.ticker),
      side,
      el("td", "num", fields.notional),
      el("td", `num ${tone(fields.pnl)}`, fields.tradePnl),
      el("td", `num ${tone(fields.runningPnlValue)}`, fields.runningPnl),
      el("td", `num ${tone(fields.runningBalanceValue)}`, fields.runningBalance),
      renderWhyCell(fields, sleeve)
    );
    tbody.append(tr);
  }
  table.append(caption, thead, tbody);
  wrap.append(table);
  panel.append(wrap, note);
  section.append(panel);
  return section;
}

function render(summaryRows, tradeRows, meta) {
  const sleeves = sortSleeves(Array.isArray(summaryRows) ? summaryRows : [], undefined);
  renderStatus(meta || {}, latestStamp(sleeves.map((row) => row.asOf)));
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
    const rows = (grouped.get(key) || []).slice().sort((a, b) => {
      const da = parseTime(tradeStamp(a));
      const db = parseTime(tradeStamp(b));
      const am = da ? da.getTime() : Number.NEGATIVE_INFINITY;
      const bm = db ? db.getTime() : Number.NEGATIVE_INFINITY;
      if (am !== bm) return bm - am;
      return String(b.timestamp_et || "").localeCompare(String(a.timestamp_et || ""));
    });
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

function renderModels(payload, oosPayload, meta) {
  modelsEl.replaceChildren();
  const models = payload && Array.isArray(payload.models) ? payload.models.slice() : [];
  const exported = oosRows(oosPayload);
  const freshBits = [];
  if (meta?.fetched_at) freshBits.push(`Last refreshed ${formatEt(meta.fetched_at)}`);
  if (payload?.as_of) freshBits.push(`Models as of ${formatEt(payload.as_of)}`);
  if (oosPayload?.updated_at) freshBits.push(`OOS updated ${formatEt(oosPayload.updated_at)}`);
  if (freshBits.length) modelsEl.append(el("p", "freshness", freshBits.join(" · ")));
  if (payload && payload.note) modelsEl.append(el("p", "note", String(payload.note)));
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
  let meta = {};
  try {
    meta = await loadJson("data/meta.json", Date.now());
  } catch {
    meta = {
      export_status: "error",
      export_error: "data/meta.json did not load. The board cannot confirm this snapshot is fresh.",
    };
  }
  const token = meta.fetched_at || meta.export_attempted_at || Date.now();
  try {
    const [summary, trades, models, oos] = await Promise.all([
      loadJson("data/kpi_summary.json", token),
      loadJson("data/kpi_trades_scrubbed.json", token),
      loadJson("data/models.json", token).catch(() => ({ models: [], note: "data/models.json is not in this snapshot." })),
      loadJson("data/models_oos.json", token).catch(() => ({ rows: [] })),
    ]);
    render(summary, trades, meta);
    renderModels(models, oos, meta);
  } catch (error) {
    renderStatus(meta);
    const lines = statusEl.querySelector(".status-lines") || statusEl;
    lines.append(el("p", "status-copy status-error", "KPI JSON did not load."));
    boardEl.replaceChildren(el("p", "empty", String(error.message || error)));
  }
}

main();
