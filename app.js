import { buildChart, sampleAt } from "./curves.js";
import {
  closedFillStats,
  cryptoOosModels,
  deriveSleeve,
  feeDragFromTrades,
  formatPct,
  formatUsd,
  formatWinPct,
  formatWinRecord,
  headroomFill,
  inferLiveBackend,
  labelFor,
  money,
  sleeveKey,
  sortSleeves,
  tone,
  winStats,
  winTone,
} from "./derive.js";
import { posOpenKey, positionRows, positionsFor, sortPositions } from "./positions.js";
import { csvFilename, linkSegments, preferredWhy, tapeCsv, tapeOpenKey } from "./tape.js";

const statusEl = document.querySelector("#status");
const boardEl = document.querySelector("#board");
const tapesEl = document.querySelector("#tapes");
const positionsEl = document.querySelector("#positions");
const curvesEl = document.querySelector("#curves");
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

function renderSleeve(derived, { hero = false, trades = [] } = {}) {
  const card = el("article", `sleeve ${derived.sleeve}${hero ? " hero" : ""}`);
  const head = el("header", "sleeve-head");
  head.append(el("h2", null, derived.label));
  if (derived.asOf) {
    const when = el("time", null, formatEt(derived.asOf));
    when.dateTime = String(derived.asOf);
    head.append(when);
  }
  card.append(head);

  const status = sleeveStatus(derived.sleeve);
  if (status) head.append(el("p", `chip ${status.className}`, status.label));
  const grid = el("div", "metrics");
  const balance = metric(
    "Running balance (book)",
    formatUsd(derived.runningBalance),
    tone(derived.runningBalance)
  );
  balance.append(el("p", "fine", "book = start + realized + unrealized"));
  const start = metric("Start", formatUsd(derived.seed));
  start.append(el("p", "fine", seedFine(derived.sleeve)));
  grid.append(
    splitPnl("Realized P&L", derived.realizedPnl, derived.realizedPnlFrac, "closed exits"),
    splitPnl("Unrealized P&L", derived.unrealizedPnl, derived.unrealizedPnlFrac, "open MTM"),
    balance,
    start
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

  const stats = winStats(trades, derived.sleeve);
  const win = metric("Win %", formatWinPct(stats.rate), winTone(stats));
  win.append(el("p", "fine", formatWinRecord(stats)));
  grid.append(win);

  card.append(grid);
  if (derived.note) card.append(el("p", "note", String(derived.note)));
  const realign = sleeveRealignNote(derived.sleeve);
  if (realign) card.append(el("p", "note", realign));
  return card;
}

function sleeveStatus(sleeve) {
  if (sleeve === "crypto") return { className: "live", label: "Live" };
  if (sleeve === "equities") return { className: "paused", label: "Paused" };
  if (sleeve === "combined") return { className: "paused", label: "Legacy sum" };
  return null;
}

function seedFine(sleeve) {
  if (sleeve === "crypto") return "Scrubbed seed on this row. Not a new full-book dollar amount.";
  if (sleeve === "equities") return "Legacy scrubbed seed. The desk is paused.";
  if (sleeve === "combined") return "Legacy sum of the scrubbed sleeve seeds.";
  return "Scrubbed seed on this row.";
}

function sleeveRealignNote(sleeve) {
  if (sleeve === "crypto") {
    return "Crypto is the live sleeve. The \u221210% kill and +2.5% target stay the rails for the full Agentic book after the equity flatten. Dollars on this card use the scrubbed seed already in the snapshot. PBR, BA, and AIG are still queued for regular hours.";
  }
  if (sleeve === "equities") {
    return "Paused. Equity modeling and place are not live. Crypto owns the Agentic book. Open names are an unwind, queued to flat.";
  }
  if (sleeve === "combined") {
    return "Legacy sum while the equity flatten is still open. Crypto is the live sleeve. This row is not a second book in trade.";
  }
  return "";
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

function feeText(seed, frac) {
  if (frac == null) return "\u2014";
  const dollars = seedTimes(seed, frac);
  return dollars == null ? "\u2014" : formatUsd(dollars);
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

function storedPosOpen(sleeve) {
  try {
    const value = localStorage.getItem(posOpenKey(sleeve));
    if (value === "closed") return false;
    if (value === "open") return true;
  } catch {
    /* localStorage can throw in private mode. Default to open. */
  }
  return true;
}

function storePosOpen(sleeve, open) {
  try {
    localStorage.setItem(posOpenKey(sleeve), open ? "open" : "closed");
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
    fee: feeText(seed, readFrac(trade, "fee_frac_of_book")),
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
  if (sleeve === "equities") {
    section.append(
      el(
        "p",
        "note",
        "Paused tape. Equity place is not live. Crypto owns the Agentic book. PBR, BA, and AIG are still queued for regular hours."
      )
    );
  }

  if (!trades.length) {
    panel.append(el("p", "empty", "No scrubbed fills."));
    section.append(panel);
    return section;
  }

  const wrap = el("div", "table-wrap");
  const table = el("table");
  const captionText = `${labelFor(sleeve)} fills. Fee, trade P&L, running P&L, and running balance are the book seed times the fraction. Fee is fee_frac_of_book. Running balance is start plus realized P&L through that fill.`;
  const caption = el("caption", "sr-only", `${labelFor(sleeve)} fills`);
  const note = el("p", "tape-note", captionText);
  note.id = `tape-note-${sleeve}`;
  table.setAttribute("aria-describedby", note.id);
  const thead = el("thead");
  const headRow = el("tr");
  for (const label of ["Time", "Ticker", "Side", "Notional", "Fee", "Trade P&L", "Running P&L", "Running balance", "Why"]) {
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
      el("td", "num", fields.fee),
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
  const trades = Array.isArray(tradeRows) ? tradeRows : [];
  renderStatus(meta || {}, latestStamp(sleeves.map((row) => row.asOf)));
  boardEl.replaceChildren();
  const combined = sleeves.find((row) => row.sleeve === "combined");
  const rest = sleeves.filter((row) => row.sleeve !== "combined");
  if (combined) boardEl.append(renderSleeve(combined, { hero: true, trades }));
  const grid = el("div", "sleeve-grid");
  for (const row of rest) grid.append(renderSleeve(row, { trades }));
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

function formatPrice(value) {
  const n = Number(value);
  if (!Number.isFinite(n)) return "\u2014";
  const abs = Math.abs(n);
  const digits = abs >= 1000 ? 2 : abs >= 1 ? 4 : 8;
  return `$${n.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: digits })}`;
}

function formatQty(value) {
  const n = Number(value);
  if (!Number.isFinite(n)) return value ? String(value) : "\u2014";
  return n.toLocaleString("en-US", { maximumFractionDigits: 8 });
}

const RTH_UNWIND = new Set(["PBR", "BA", "AIG"]);

function unwindLabel(ticker) {
  return RTH_UNWIND.has(String(ticker || "").trim().toUpperCase()) ? "Queued RTH" : "Unwind";
}

function renderPositionTable(title, rows, { showSleeve, sleeve, unwind = false }) {
  const section = el("section", "pos");
  const heading = el("h3");
  const toggle = document.createElement("button");
  toggle.type = "button";
  toggle.className = "tape-toggle";
  const panelId = `pos-panel-${sleeve}`;
  toggle.setAttribute("aria-controls", panelId);
  const open = storedPosOpen(sleeve);
  toggle.setAttribute("aria-expanded", open ? "true" : "false");
  toggle.append(document.createTextNode(title));
  const chevron = el("span", "chevron");
  chevron.setAttribute("aria-hidden", "true");
  toggle.append(chevron);
  heading.append(toggle);

  const panel = el("div", "pos-panel");
  panel.id = panelId;
  panel.hidden = !open;
  toggle.addEventListener("click", () => {
    const next = toggle.getAttribute("aria-expanded") !== "true";
    toggle.setAttribute("aria-expanded", next ? "true" : "false");
    panel.hidden = !next;
    storePosOpen(sleeve, next);
  });
  section.append(heading);
  if (!rows.length) {
    panel.append(el("p", "empty", "No open positions."));
    section.append(panel);
    return section;
  }
  const wrap = el("div", "table-wrap");
  const table = el("table");
  const thead = el("thead");
  const headRow = el("tr");
  const labels = showSleeve
    ? ["Sleeve", "Ticker", "Side", "Qty", "Avg", "Mark", "Unrealized"]
    : ["Ticker", "Side", "Qty", "Avg", "Mark", "Unrealized"];
  if (unwind) labels.splice(showSleeve ? 2 : 1, 0, "Unwind");
  const numeric = new Set(["Qty", "Avg", "Mark", "Unrealized"]);
  for (const label of labels) {
    const th = el("th", numeric.has(label) ? "num" : "", label);
    th.scope = "col";
    headRow.append(th);
  }
  thead.append(headRow);
  const tbody = el("tbody");
  for (const row of rows) {
    const shaped = deriveSleeve({ sleeve: row.sleeve });
    const frac = readFrac(row, "unrealized_pnl_frac");
    const dollars = money(shaped.seed, frac);
    const tr = el("tr");
    const sideName = String(row.side || "").toLowerCase();
    const sideCell = el("td");
    sideCell.append(el("span", `pill ${sideName}`, sideName || "\u2014"));
    const unrealized = el(
      "td",
      `num ${tone(dollars)}`,
      dollars == null ? "\u2014" : `${formatUsd(dollars, { signed: true })} (${formatPct(frac, { signed: true, digits: 2 })})`
    );
    const cells = [];
    if (showSleeve) {
      const sleeveLabel = shaped.sleeve === "equities" ? "Equities \u00b7 unwind" : labelFor(shaped.sleeve);
      cells.push(el("td", null, sleeveLabel));
    }
    cells.push(el("td", "ticker", String(row.ticker)));
    if (unwind) cells.push(el("td", null, unwindLabel(row.ticker)));
    cells.push(
      sideCell,
      el("td", "num", formatQty(row.qty)),
      el("td", "num", formatPrice(row.avg)),
      el("td", "num", formatPrice(row.mark)),
      unrealized
    );
    tr.append(...cells);
    tbody.append(tr);
  }
  table.append(thead, tbody);
  wrap.append(table);
  panel.append(wrap);
  section.append(panel);
  return section;
}

function renderPositions(payload) {
  if (!positionsEl) return;
  positionsEl.replaceChildren();
  if (!payload || payload.missing) {
    positionsEl.append(el("p", "empty", "Open positions are not in this snapshot yet."));
    return;
  }
  const rows = sortPositions(positionRows(payload));
  positionsEl.append(
    el(
      "p",
      "note",
      "Equities modeling and place are paused. Crypto owns the Agentic book. Open equity names are an unwind, queued to flat. PBR, BA, and AIG are still queued for regular hours. The export has no per-fill unwind flag, so this label is the desk state."
    ),
    renderPositionTable("Combined", positionsFor(rows, "combined"), { showSleeve: true, sleeve: "combined" }),
    renderPositionTable("Crypto", positionsFor(rows, "crypto"), { showSleeve: false, sleeve: "crypto" }),
    renderPositionTable("Equities", positionsFor(rows, "equities"), {
      showSleeve: false,
      sleeve: "equities",
      unwind: true,
    })
  );
}

const SVG_NS = "http://www.w3.org/2000/svg";
let curveMode = "all";
let curvePayload = { series: [] };

function svgEl(name, attrs) {
  const node = document.createElementNS(SVG_NS, name);
  for (const [key, value] of Object.entries(attrs || {})) node.setAttribute(key, String(value));
  return node;
}

function curveReadout(sample) {
  if (!sample) return "No sleeve history in this snapshot.";
  const bits = [];
  for (const sleeve of ["combined", "crypto", "equities"]) {
    if (sample.values[sleeve] == null) continue;
    if (curveMode !== "all" && sleeve !== curveMode) continue;
    const shaped = deriveSleeve({ sleeve });
    const dollars = money(shaped.seed, sample.values[sleeve]);
    bits.push(
      `${labelFor(sleeve)} ${formatUsd(dollars)} (${formatPct(sample.values[sleeve] - 1, { signed: true, digits: 2 })})`
    );
  }
  const when = formatEt(sample.asOf);
  return [when, bits.join(" · ")].filter(Boolean).join(" · ");
}

function paintCurves() {
  if (!curvesEl) return;
  curvesEl.replaceChildren();
  if (!curvePayload || curvePayload.missing) {
    curvesEl.append(el("p", "empty", "Sleeve history is not in this snapshot yet."));
    return;
  }
  const chart = buildChart(curvePayload, curveMode);
  if (chart.empty) {
    curvesEl.append(
      el("p", "empty", curveMode === "all" ? "No sleeve history in this snapshot." : "No points for this series.")
    );
    return;
  }
  const frame = el("div", "chart-frame");
  const svg = svgEl("svg", { viewBox: `0 0 ${chart.width} ${chart.height}`, role: "img" });
  const latest = sampleAt(chart, 1);
  const title = svgEl("title");
  title.textContent = `Equity curves. ${curveReadout(latest)}`;
  svg.append(title);
  for (const tick of chart.ticks) {
    svg.append(
      svgEl("line", {
        x1: chart.plot.left,
        x2: chart.plot.right,
        y1: tick.y,
        y2: tick.y,
        stroke: "rgba(244,239,230,0.12)",
      })
    );
    const label = svgEl("text", {
      x: chart.plot.left - 8,
      y: tick.y + 4,
      "text-anchor": "end",
      fill: "#b3a794",
      "font-size": "11",
      "font-family": "Outfit, sans-serif",
    });
    label.textContent = tick.frac.toFixed(3);
    svg.append(label);
  }
  const t0 = formatEt(new Date(chart.t0).toISOString());
  const t1 = formatEt(new Date(chart.t1).toISOString());
  const x0 = svgEl("text", {
    x: chart.plot.left,
    y: chart.height - 8,
    fill: "#b3a794",
    "font-size": "11",
    "font-family": "Outfit, sans-serif",
  });
  x0.textContent = t0;
  const x1 = svgEl("text", {
    x: chart.plot.right,
    y: chart.height - 8,
    "text-anchor": "end",
    fill: "#b3a794",
    "font-size": "11",
    "font-family": "Outfit, sans-serif",
  });
  x1.textContent = t1;
  svg.append(x0, x1);
  for (const series of chart.series) {
    svg.append(
      svgEl("polyline", {
        fill: "none",
        stroke: series.color,
        "stroke-width": "2.25",
        "stroke-linejoin": "round",
        "stroke-linecap": "round",
        points: series.points.map((point) => `${point.x.toFixed(2)},${point.y.toFixed(2)}`).join(" "),
      })
    );
    const last = series.points[series.points.length - 1];
    svg.append(svgEl("circle", { cx: last.x, cy: last.y, r: 3.5, fill: series.color }));
  }
  const readout = el("p", "curve-readout", curveReadout(latest));
  svg.addEventListener("pointermove", (event) => {
    const point = svg.createSVGPoint();
    point.x = event.clientX;
    point.y = event.clientY;
    const ctm = svg.getScreenCTM();
    if (!ctm) return;
    const loc = point.matrixTransform(ctm.inverse());
    const span = chart.plot.right - chart.plot.left;
    const ratio = span ? (loc.x - chart.plot.left) / span : 0;
    readout.textContent = curveReadout(sampleAt(chart, ratio));
  });
  svg.addEventListener("pointerleave", () => {
    readout.textContent = curveReadout(latest);
  });
  frame.append(svg);
  const legend = el("div", "legend");
  for (const series of chart.series) {
    const item = el("span");
    const swatch = el("i");
    swatch.style.background = series.color;
    item.append(swatch, document.createTextNode(labelFor(series.sleeve)));
    legend.append(item);
  }
  curvesEl.append(frame, legend, readout);
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

function fixedDigits(value, digits) {
  const n = Number(value);
  if (!Number.isFinite(n)) return "\u2014";
  return n.toFixed(digits);
}

function feeView(scorecard, trades) {
  const fromTape = feeDragFromTrades(trades, "crypto");
  const published = scorecard?.fee_drag;
  const drag = published?.status === "known" ? published : fromTape;
  if (!drag || drag.status !== "known" || (drag.fee_usd == null && drag.sell_fee_usd == null)) {
    return {
      value: "UNKNOWN",
      toneClass: "flat",
      note: published?.note || "No fee column on the scrubbed tape. Not estimated.",
    };
  }
  const total = drag.fee_usd != null ? drag.fee_usd : drag.sell_fee_usd;
  let note = drag.note || "Crypto fee dollars from the rows that were read.";
  if (drag.sell_fee_usd != null && drag.fee_usd != null && drag.sell_fee_usd !== drag.fee_usd) {
    note = `${note} Closed-sell fees ${formatUsd(drag.sell_fee_usd)}.`;
  }
  return {
    value: formatUsd(total),
    toneClass: total > 0 ? "down" : "flat",
    note,
  };
}

function backendView(scorecard, models, oosPayload) {
  const published = scorecard?.live_backend;
  if (published && (published.id || published.note)) return published;
  const inferred = inferLiveBackend(models);
  const promoted = cryptoOosModels(oosPayload).find((row) => row.promoted === true);
  if (inferred.id === "rules" && promoted?.model) {
    return {
      ...inferred,
      promoted_model: promoted.model,
      promoted_in_use: false,
      note: `The CLI is still --backend rules. ${promoted.model} is promoted on after-cost mean and is not the live backend.`,
    };
  }
  return inferred;
}

function summaryRows(summary) {
  if (Array.isArray(summary)) return summary;
  if (summary && Array.isArray(summary.rows)) return summary.rows;
  return [];
}

function renderCryptoScorecard(models, oosPayload, summary, trades, scorecard) {
  const card = el("article", "sleeve crypto model-card model-scorecard");
  card.id = "crypto-scorecard";
  const head = el("header", "sleeve-head");
  head.append(el("h2", null, "Crypto scorecard"));
  head.append(el("p", "fine", "Closed fills, rails, and the published out-of-sample read"));
  card.append(head);

  const grid = el("div", "metrics");
  const backend = backendView(scorecard, models, oosPayload);
  const backendMetric = metric("Live backend", backend.id || "UNKNOWN", "flat");
  backendMetric.append(el("p", "fine", backend.note || "Live backend is not stated in this snapshot."));
  grid.append(backendMetric);

  const fills = closedFillStats(trades, "crypto");
  const win = metric("Win %", formatWinPct(fills.rate), winTone(fills));
  win.append(el("p", "fine", `${formatWinRecord(fills)} closed sells`));
  grid.append(win);

  const expectancy = metric("Expectancy");
  const expectancyValue = expectancy.querySelector(".v");
  expectancyValue.classList.add(tone(fills.expectancyFrac));
  expectancyValue.replaceChildren(
    pair(
      fills.expectancyFrac == null ? "\u2014" : formatPct(fills.expectancyFrac, { signed: true, digits: 2 }),
      fills.expectancyUsd == null ? "" : formatUsd(fills.expectancyUsd, { signed: true })
    )
  );
  expectancy.append(el("p", "fine", `Mean P&L of decided crypto sells. Seed $${fills.seed}.`));
  grid.append(expectancy);

  const fee = feeView(scorecard, trades);
  const feeMetric = metric("Fee drag", fee.value, fee.toneClass);
  feeMetric.append(el("p", "fine", fee.note));
  grid.append(feeMetric);

  const cryptoRow = summaryRows(summary).find((row) => sleeveKey(row) === "crypto") || null;
  const derived = cryptoRow ? deriveSleeve(cryptoRow) : null;
  const headroom = metric("Kill headroom");
  const headroomValue = headroom.querySelector(".v");
  if (!derived || derived.killHeadroomFrac == null) {
    headroomValue.textContent = "\u2014";
  } else {
    headroomValue.replaceChildren(
      pair(`${formatPct(derived.killHeadroomFrac)} of book`, formatUsd(derived.killHeadroom))
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
  }
  const rails = [];
  if (derived?.dayKillFrac != null) {
    rails.push(`Day kill ${formatPct(derived.dayKillFrac)} (${formatUsd(derived.dayKill)})`);
  }
  if (derived?.dayTargetFrac != null) {
    rails.push(
      `Day target ${formatPct(derived.dayTargetFrac, { signed: true })} (${formatUsd(derived.dayTarget, { signed: true })}), realized only`
    );
  }
  headroom.append(el("p", "fine", rails.join(". ") || "Day rails are not in this snapshot."));
  grid.append(headroom);
  card.append(grid);

  const unique = scorecard?.order_id_unique;
  let fillNote = fills.orderIdAvailable
    ? `Duplicate order ids dropped: ${fills.deduped}. Flat exits excluded. T24b will improve joined-fill metrics.`
    : "Same rules as sleeve Win %: sell, finite P&L, flat zero excluded. The scrubbed tape has no order id, so rows are not collapsed. T24b will improve joined-fill metrics.";
  if (
    unique &&
    Number.isFinite(unique.wins) &&
    Number.isFinite(unique.losses) &&
    (unique.wins !== fills.wins || unique.losses !== fills.losses)
  ) {
    fillNote = `Warehouse order-id unique closed sells are ${unique.wins}\u2013${unique.losses}. The headline matches sleeve Win % on the scrubbed tape. T24b will improve joined-fill metrics.`;
  }
  card.append(el("p", "note", fillNote));

  const oosModels = cryptoOosModels(oosPayload);
  const oosNote = oosPayload?.note || scorecard?.oos?.note;
  const feeBps = scorecard?.oos?.fee_bps ?? 30;
  if (!oosModels.length) {
    card.append(el("p", "note", "No crypto out-of-sample rows in this snapshot."));
  } else {
    const wrap = el("div", "score-scroll");
    const table = el("table", "score-oos");
    const thead = document.createElement("thead");
    const headerRow = document.createElement("tr");
    for (const label of ["Model", "AUC", "After cost", "IR vs BTC", "n long", "Promoted"]) {
      headerRow.append(el("th", null, label));
    }
    thead.append(headerRow);
    const tbody = document.createElement("tbody");
    for (const row of oosModels) {
      const line = document.createElement("tr");
      const promoted = row.promoted === true ? "yes" : row.promoted === false ? "no" : "\u2014";
      for (const text of [
        String(row.model || "model"),
        fixedDigits(row.auc, 4),
        fixedDigits(row.after_cost_mean, 4),
        fixedDigits(row.sleeve_ir_vs_spy, 4),
        row.n_long == null ? "\u2014" : String(row.n_long),
        promoted,
      ]) {
        line.append(el("td", null, text));
      }
      tbody.append(line);
    }
    table.append(thead, tbody);
    wrap.append(table);
    card.append(wrap);
  }
  const benchmark = oosPayload?.benchmark_note || scorecard?.oos?.benchmark_note;
  if (benchmark) card.append(el("p", "fine", String(benchmark)));
  card.append(
    el(
      "p",
      "note",
      oosNote && String(oosNote).includes("30 bp")
        ? String(oosNote)
        : `Out-of-sample after-cost uses ${feeBps} bp. Measured live fee ~95 bps/leg (median) / ~190 RT from tape. T24d will set FEE_BPS from that.`
    )
  );

  const train = scorecard?.last_train || {};
  const trainBits = [];
  const modelsAsOf = train.models_as_of || models?.as_of;
  const oosAsOf = train.oos_updated_at || oosPayload?.updated_at;
  if (modelsAsOf) trainBits.push(`Models as of ${formatEt(modelsAsOf)}`);
  if (oosAsOf) trainBits.push(`OOS updated ${formatEt(oosAsOf)}`);
  if (train.trained_at) trainBits.push(`Trained ${formatEt(train.trained_at)}`);
  if (train.promoted_at) trainBits.push(`Promoted ${formatEt(train.promoted_at)}`);
  trainBits.push(train.note || "No separate train or promote timestamp is in the exported model files.");
  card.append(el("p", "fine", trainBits.join(". ").replace(/\.\./g, ".")));
  card.append(renderSignalLinkage(scorecard));
  return card;
}

function renderSignalLinkage(scorecard) {
  const link = scorecard?.signal_linkage;
  const known = link?.status === "known";
  const artifacts = known && link.artifact_count != null ? String(link.artifact_count) : "UNKNOWN";
  const outcomes = known && link.outcome_count != null ? String(link.outcome_count) : "UNKNOWN";
  const generated = link?.last_generated_at ? formatEt(link.last_generated_at) : "UNKNOWN";
  return el(
    "p",
    "note",
    `Live scores land in signal_artifacts (${artifacts}). Fills join on signal_trade_outcomes (${outcomes}). Last generated ${generated}.`
  );
}

function renderModels(payload, oosPayload, meta, summary, trades, scorecard) {
  modelsEl.replaceChildren();
  const models = payload && Array.isArray(payload.models) ? payload.models.slice() : [];
  const exported = oosRows(oosPayload);
  const freshBits = [];
  if (meta?.fetched_at) freshBits.push(`Last refreshed ${formatEt(meta.fetched_at)}`);
  if (payload?.as_of) freshBits.push(`Models as of ${formatEt(payload.as_of)}`);
  if (oosPayload?.updated_at) freshBits.push(`OOS updated ${formatEt(oosPayload.updated_at)}`);
  if (freshBits.length) modelsEl.append(el("p", "freshness", freshBits.join(" · ")));
  if (payload && payload.note) modelsEl.append(el("p", "note", String(payload.note)));
  modelsEl.append(renderCryptoScorecard(payload, oosPayload, summary, trades, scorecard));
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
  const anchored = new Set();
  for (const model of models) {
    const mine = exported.filter((row) => sameSleeve(row, String(model.sleeve || "").toLowerCase()));
    for (const row of mine) used.add(row);
    const card = el("article", "sleeve model-card");
    const sleeveName = String(model.sleeve || "").toLowerCase();
    if ((sleeveName === "crypto" || sleeveName === "equities") && !anchored.has(sleeveName)) {
      card.id = `model-${sleeveName}`;
      anchored.add(sleeveName);
    }
    const head = el("header", "sleeve-head");
    head.append(el("h2", null, model.name || "Model"));
    if (model.sleeve) head.append(el("p", "fine", String(model.sleeve)));
    if (sleeveName === "equities") head.append(el("p", "chip paused", "Paused"));
    card.append(head);
    if (sleeveName === "equities") {
      card.append(
        el(
          "p",
          "note",
          "Paused. Equity modeling and place are not live. Crypto owns the Agentic book. This card is the last published research read."
        )
      );
    }
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

function focusModel(hash) {
  const id = String(hash || "").replace(/^#/, "");
  if (!id || id === "models") return;
  const node = document.getElementById(id);
  if (node) node.scrollIntoView({ block: "start" });
}

function openFromHash() {
  const hash = location.hash;
  if (hash === "#models" || hash.startsWith("#model-")) {
    showTab("models");
    focusModel(hash);
  }
}

async function main() {
  tabSleeves.addEventListener("click", () => {
    showTab("sleeves");
    if (location.hash.startsWith("#model")) history.pushState(null, "", `${location.pathname}${location.search}`);
  });
  tabModels.addEventListener("click", () => {
    showTab("models");
    history.pushState(null, "", "#models");
  });
  document.querySelectorAll('a[data-tab="models"]').forEach((anchor) => {
    anchor.addEventListener("click", () => {
      showTab("models");
      requestAnimationFrame(() => focusModel(anchor.getAttribute("href")));
    });
  });
  document.querySelectorAll("[data-curve]").forEach((button) => {
    button.addEventListener("click", () => {
      curveMode = button.getAttribute("data-curve") || "all";
      document.querySelectorAll("[data-curve]").forEach((other) => {
        other.setAttribute("aria-pressed", other === button ? "true" : "false");
      });
      paintCurves();
    });
  });
  window.addEventListener("hashchange", openFromHash);
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
    const [summary, trades, models, oos, positions, curves, scorecard] = await Promise.all([
      loadJson("data/kpi_summary.json", token),
      loadJson("data/kpi_trades_scrubbed.json", token),
      loadJson("data/models.json", token).catch(() => ({ models: [], note: "data/models.json is not in this snapshot." })),
      loadJson("data/models_oos.json", token).catch(() => ({ rows: [] })),
      loadJson("data/open_positions.json", token).catch(() => ({ missing: true, positions: [] })),
      loadJson("data/sleeve_curves.json", token).catch(() => ({ missing: true, series: [] })),
      loadJson("data/model_scorecard.json", token).catch(() => null),
    ]);
    render(summary, trades, meta);
    renderPositions(positions);
    curvePayload = curves;
    paintCurves();
    renderModels(models, oos, meta, summary, trades, scorecard);
    openFromHash();
  } catch (error) {
    renderStatus(meta);
    const lines = statusEl.querySelector(".status-lines") || statusEl;
    lines.append(el("p", "status-copy status-error", "KPI JSON did not load."));
    boardEl.replaceChildren(el("p", "empty", String(error.message || error)));
    if (positionsEl) positionsEl.replaceChildren(el("p", "empty", "Open positions did not load."));
    if (curvesEl) curvesEl.replaceChildren(el("p", "empty", "Sleeve history did not load."));
  }
}

main();
