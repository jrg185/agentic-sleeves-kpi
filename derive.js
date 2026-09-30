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
