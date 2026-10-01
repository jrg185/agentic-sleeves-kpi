// Dollar display is seed × fraction. This file never talks to Supabase.

export const SEEDS_USD = {
  crypto: 300,
  equities: 500,
  combined: 800,
};

const SLEEVE_ORDER = ["combined", "crypto", "equities"];

export function num(value) {
  if (value == null || value === "") return null;
  if (typeof value === "number") return Number.isFinite(value) ? value : null;
  const cleaned = String(value).replace(/[$,%\s,]/g, "").replace(/^\((.*)\)$/, "-$1");
  if (cleaned === "" || cleaned === "-" || cleaned === "—") return null;
  const n = Number(cleaned);
  return Number.isFinite(n) ? n : null;
}

// Rails may arrive as fractions (-0.10) or percent points (-10).
export function asFraction(value) {
  const n = num(value);
  if (n == null) return null;
  return Math.abs(n) > 1 ? n / 100 : n;
}

export function pick(row, keys) {
  if (!row) return null;
  for (const key of keys) {
    if (row[key] != null && row[key] !== "") return row[key];
  }
  return null;
}

export function sleeveKey(row) {
  return String(pick(row, ["sleeve", "book", "desk"]) || "")
    .trim()
    .toLowerCase();
}

export function seedFor(row, seeds = SEEDS_USD) {
  const explicit = num(pick(row, ["start", "seed", "start_usd", "seed_usd", "book_usd"]));
  if (explicit != null) return explicit;
  const key = sleeveKey(row);
  if (key === "combined") {
    const crypto = seeds.crypto ?? 0;
    const equities = seeds.equities ?? 0;
    if (crypto || equities) return crypto + equities;
  }
  return seeds[key] ?? null;
}

export function money(seed, frac) {
  if (seed == null || frac == null) return null;
  return Math.round((seed * frac + Number.EPSILON) * 100) / 100;
}

function fractionFrom(row, fracKeys, dollarKeys, seed, { percentPoints = false } = {}) {
  const rawFrac = pick(row, fracKeys);
  if (rawFrac != null && rawFrac !== "") return percentPoints ? asFraction(rawFrac) : num(rawFrac);
  const dollars = num(pick(row, dollarKeys));
  if (dollars == null || seed == null || seed === 0) return null;
  return dollars / seed;
}

export function deriveSleeve(row, seeds = SEEDS_USD) {
  const seed = seedFor(row, seeds);
  // Book / start can be above 1 (crypto 323.77/300). Do not treat that as percent points.
  const runningBalanceFrac = fractionFrom(
    row,
    ["running_balance_frac", "running_bal_vs_start", "balance_frac", "bal_frac", "running_bal_frac"],
    ["running_balance", "running_balance_usd", "balance_usd"],
    seed
  );
  const runningPnlFrac = fractionFrom(
    row,
    ["running_pnl_frac", "pnl_pct_of_book", "pnl_frac", "running_pnl_pct"],
    ["running_pnl_usd", "pnl_usd", "running_pnl"],
    seed,
    { percentPoints: true }
  );
  // Named _frac fields stay fractions even above 1. Cards label these apart from running P&L.
  const realizedPnlFrac = fractionFrom(
    row,
    ["realized_pnl_frac"],
    ["realized_pnl_usd", "realized_pnl"],
    seed
  );
  const unrealizedPnlFrac = fractionFrom(
    row,
    ["unrealized_pnl_frac"],
    ["unrealized_pnl_usd", "unrealized_pnl"],
    seed
  );
  const dayPnlFrac = fractionFrom(
    row,
    ["day_pnl_frac", "day_pnl_pct"],
    ["day_pnl_usd", "day_pnl"],
    seed,
    { percentPoints: true }
  );
  const dayKillFrac = fractionFrom(
    row,
    ["day_kill_pct", "kill_pct", "day_kill_frac"],
    ["day_kill_usd", "day_kill_dollars", "day_kill"],
    seed,
    { percentPoints: true }
  );
  const killHeadroomFrac = fractionFrom(
    row,
    ["kill_headroom_frac", "headroom_frac", "kill_headroom_pct"],
    ["kill_headroom_usd", "kill_headroom_dollars", "kill_headroom"],
    seed,
    { percentPoints: true }
  );
  const dayTargetFrac = fractionFrom(
    row,
    ["day_target_pct", "target_pct", "day_target_frac"],
    ["day_target_usd", "day_target_dollars", "day_target"],
    seed,
    { percentPoints: true }
  );

  return {
    sleeve: sleeveKey(row),
    label: labelFor(sleeveKey(row)),
    asOf: pick(row, ["as_of", "asof", "snapshot_at", "updated_at"]),
    note: pick(row, ["note", "notes", "day_target_note"]),
    seed,
    runningBalanceFrac,
    runningPnlFrac,
    realizedPnlFrac,
    unrealizedPnlFrac,
    dayPnlFrac,
    dayKillFrac,
    killHeadroomFrac,
    dayTargetFrac,
    runningBalance: money(seed, runningBalanceFrac),
    runningPnl: money(seed, runningPnlFrac),
    realizedPnl: money(seed, realizedPnlFrac),
    unrealizedPnl: money(seed, unrealizedPnlFrac),
    dayPnl: money(seed, dayPnlFrac),
    dayKill: money(seed, dayKillFrac),
    killHeadroom: money(seed, killHeadroomFrac),
    dayTarget: money(seed, dayTargetFrac),
  };
}

export function labelFor(sleeve) {
  if (sleeve === "crypto") return "Crypto";
  if (sleeve === "equities") return "Equities";
  if (sleeve === "combined") return "Combined";
  return sleeve ? sleeve.charAt(0).toUpperCase() + sleeve.slice(1) : "Book";
}

export function sortSleeves(rows, seeds) {
  const derived = rows.map((row) => deriveSleeve(row, seeds));
  return derived.sort((a, b) => {
    const ai = SLEEVE_ORDER.indexOf(a.sleeve);
    const bi = SLEEVE_ORDER.indexOf(b.sleeve);
    return (ai === -1 ? 99 : ai) - (bi === -1 ? 99 : bi);
  });
}

export function formatUsd(value, { signed = false } = {}) {
  if (value == null || Number.isNaN(value)) return "—";
  const abs = Math.abs(value).toLocaleString("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
  if (value < 0) return `-$${abs}`;
  if (signed && value > 0) return `+$${abs}`;
  return `$${abs}`;
}

export function formatPct(frac, { signed = false, digits = 1 } = {}) {
  if (frac == null || Number.isNaN(frac)) return "—";
  const pct = frac * 100;
  const body = `${Math.abs(pct).toFixed(digits)}%`;
  if (pct < 0) return `-${body}`;
  if (signed && pct > 0) return `+${body}`;
  return body;
}

// Width of the kill-headroom meter, as a percent of the track.
// Matches the stated headroom fraction of book (the percent on the card).
// Not rescaled by the day-kill rail, and not inverted into kill already used.
export function headroomFill(derived) {
  if (derived.killHeadroomFrac == null || Number.isNaN(derived.killHeadroomFrac)) return null;
  return Math.max(0, Math.min(100, derived.killHeadroomFrac * 100));
}

export function tone(value) {
  if (value == null || value === 0) return "flat";
  return value > 0 ? "up" : "down";
}

function exitPnlFrac(trade) {
  if (!trade) return null;
  const primary = num(trade.pnl_frac_of_book);
  if (primary != null) return primary;
  return num(trade.pnl_frac);
}

// Closed exits only: sell fills with a finite pnl fraction.
// Flat (0) exits are excluded from wins, losses, and the denominator.
// Combined is crypto sells plus equities sells, not a third tape.
export function winStats(trades, sleeve) {
  const rows = Array.isArray(trades) ? trades : [];
  const wanted = sleeve === "combined" ? ["crypto", "equities"] : [sleeve];
  let wins = 0;
  let losses = 0;
  for (const trade of rows) {
    if (!wanted.includes(sleeveKey(trade))) continue;
    if (String(trade?.side || "").trim().toLowerCase() !== "sell") continue;
    const frac = exitPnlFrac(trade);
    if (frac == null || frac === 0) continue;
    if (frac > 0) wins += 1;
    else losses += 1;
  }
  const decided = wins + losses;
  return {
    wins,
    losses,
    rate: decided === 0 ? null : wins / decided,
  };
}

export function formatWinPct(rate) {
  return formatPct(rate, { digits: 0 });
}

export function formatWinRecord(stats) {
  const wins = stats?.wins || 0;
  const losses = stats?.losses || 0;
  return `${wins}\u2013${losses}`;
}

export function winTone(stats) {
  if (!stats || stats.rate == null) return tone(null);
  if (stats.wins === stats.losses) return tone(0);
  return tone(stats.wins - stats.losses);
}

const OOS_MODEL_ORDER = ["rules", "logistic", "lgbm"];
const FEE_KEYS = ["fee_usd", "fee", "fee_charged"];

function finiteFrac(value) {
  return num(value);
}

// Same exit rule as winStats. order id collapses duplicate sells when the
// field is actually on the row. The scrubbed Pages tape does not carry it.
export function closedFillStats(trades, sleeve, seed = SEEDS_USD.crypto) {
  const rows = Array.isArray(trades) ? trades : [];
  const wanted = sleeve === "combined" ? ["crypto", "equities"] : [sleeve];
  const seen = new Set();
  let wins = 0;
  let losses = 0;
  let flats = 0;
  let deduped = 0;
  let orderIdAvailable = false;
  let sum = 0;
  for (const trade of rows) {
    if (!wanted.includes(sleeveKey(trade))) continue;
    if (String(trade?.side || "").trim().toLowerCase() !== "sell") continue;
    const frac = exitPnlFrac(trade);
    if (frac == null) continue;
    const orderId = String(trade?.order_id || "").trim();
    if (orderId) {
      orderIdAvailable = true;
      if (seen.has(orderId)) {
        deduped += 1;
        continue;
      }
      seen.add(orderId);
    }
    if (frac === 0) {
      flats += 1;
      continue;
    }
    if (frac > 0) wins += 1;
    else losses += 1;
    sum += frac;
  }
  const decided = wins + losses;
  const expectancyFrac = decided === 0 ? null : sum / decided;
  return {
    wins,
    losses,
    flats,
    decided,
    rate: decided === 0 ? null : wins / decided,
    expectancyFrac,
    expectancyUsd: money(seed, expectancyFrac),
    seed,
    deduped,
    orderIdAvailable,
  };
}

function feeAmount(row, seed) {
  for (const key of FEE_KEYS) {
    if (row && Object.prototype.hasOwnProperty.call(row, key)) {
      const amount = finiteFrac(row[key]);
      if (amount != null) return amount;
    }
  }
  if (row && Object.prototype.hasOwnProperty.call(row, "fee_frac_of_book") && seed) {
    const frac = finiteFrac(row.fee_frac_of_book);
    if (frac != null) return frac * seed;
  }
  return null;
}

export function feeDragFromTrades(trades, sleeve = "crypto", seed = SEEDS_USD.crypto) {
  const rows = Array.isArray(trades) ? trades : [];
  const seen = new Set();
  let sawField = false;
  let numeric = false;
  let total = 0;
  let sellTotal = 0;
  let n = 0;
  for (const trade of rows) {
    if (sleeveKey(trade) !== sleeve) continue;
    const hasField =
      FEE_KEYS.some((key) => trade && Object.prototype.hasOwnProperty.call(trade, key)) ||
      Object.prototype.hasOwnProperty.call(trade || {}, "fee_frac_of_book");
    if (!hasField) continue;
    sawField = true;
    const orderId = String(trade?.order_id || "").trim();
    if (orderId) {
      if (seen.has(orderId)) continue;
      seen.add(orderId);
    }
    const amount = feeAmount(trade, seed);
    if (amount == null) continue;
    numeric = true;
    total += amount;
    n += 1;
    if (String(trade?.side || "").trim().toLowerCase() === "sell") sellTotal += amount;
  }
  if (!sawField || !numeric) return null;
  const roundUsd = (value) => Math.round((value + Number.EPSILON) * 100) / 100;
  const feeUsd = roundUsd(total);
  const sellFeeUsd = roundUsd(sellTotal);
  return {
    status: "known",
    fee_usd: feeUsd,
    sell_fee_usd: sellFeeUsd,
    fee_frac: seed ? feeUsd / seed : null,
    n,
    seed_usd: seed,
    note: "Sum of fee dollars on the crypto rows in this snapshot.",
  };
}

export function inferLiveBackend(models) {
  const list = Array.isArray(models?.models) ? models.models : [];
  const crypto = list.find((model) => String(model?.sleeve || "").trim().toLowerCase() === "crypto") || {};
  const oosNote = crypto.oos && typeof crypto.oos === "object" ? crypto.oos.note : "";
  const text = [models?.note, crypto.used, crypto.training, oosNote].filter(Boolean).join("\n");
  const rules = text.includes("--backend rules");
  return {
    id: rules ? "rules" : null,
    cli: rules ? "--backend rules" : null,
    note: rules
      ? "The CLI is still --backend rules."
      : "Live backend is not stated in data/models.json. Not inferred.",
  };
}

export function cryptoOosModels(payload) {
  const rows = Array.isArray(payload) ? payload : Array.isArray(payload?.rows) ? payload.rows : [];
  const crypto = rows.filter((row) => {
    const sleeve = String(row?.sleeve || row?.asset_class || "").trim().toLowerCase();
    return sleeve === "crypto";
  });
  return crypto.slice().sort((a, b) => {
    const ai = OOS_MODEL_ORDER.indexOf(String(a?.model || "").toLowerCase());
    const bi = OOS_MODEL_ORDER.indexOf(String(b?.model || "").toLowerCase());
    return (ai === -1 ? 99 : ai) - (bi === -1 ? 99 : bi);
  });
}
