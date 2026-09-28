// Sleeve equity curves. Fractions only. No network, no DOM, no account dollars.

export const CURVE_SLEEVES = ["combined", "crypto", "equities"];

export const CURVE_COLORS = {
  combined: "#f4efe6",
  crypto: "#e3a15a",
  equities: "#7eb8c4",
};

export function normalizeSleeve(value) {
  const key = String(value || "").trim().toLowerCase();
  if (key === "equity") return "equities";
  return CURVE_SLEEVES.includes(key) ? key : "";
}

export function curveRows(payload) {
  const rows = Array.isArray(payload) ? payload : payload?.series;
  if (!Array.isArray(rows)) return [];
  return rows.filter((row) => row && typeof row === "object" && normalizeSleeve(row.sleeve) && row.as_of);
}

export function filterRows(rows, mode) {
  const wanted = mode && mode !== "all" ? normalizeSleeve(mode) : "";
  if (!wanted) return rows.slice();
  return rows.filter((row) => normalizeSleeve(row.sleeve) === wanted);
}

function finiteFrac(row) {
  const n = Number(row?.running_balance_frac);
  return Number.isFinite(n) ? n : null;
}

function timeMs(value) {
  const ms = Date.parse(value);
  return Number.isNaN(ms) ? null : ms;
}

export function buildChart(rows, mode, size = {}) {
  const width = size.width || 640;
  const height = size.height || 260;
  const plot = {
    left: 52,
    right: width - 16,
    top: 16,
    bottom: height - 28,
  };
  const visible = filterRows(curveRows(rows), mode);
  const plotted = [];
  for (const row of visible) {
    const frac = finiteFrac(row);
    const ms = timeMs(row.as_of);
    if (frac == null || ms == null) continue;
    plotted.push({
      sleeve: normalizeSleeve(row.sleeve),
      asOf: row.as_of,
      ms,
      frac,
    });
  }
  if (!plotted.length) {
    return { empty: true, width, height, plot, series: [], yMin: null, yMax: null, ticks: [] };
  }
  const t0 = Math.min(...plotted.map((point) => point.ms));
  const t1 = Math.max(...plotted.map((point) => point.ms));
  let yMin = Math.min(...plotted.map((point) => point.frac));
  let yMax = Math.max(...plotted.map((point) => point.frac));
  if (yMin === yMax) {
    yMin -= 0.01;
    yMax += 0.01;
  }
  const pad = (yMax - yMin) * 0.08;
  yMin -= pad;
  yMax += pad;
  const span = t1 - t0;
  const ySpan = yMax - yMin;
  const xFor = (ms) => {
    const ratio = span === 0 ? 0.5 : (ms - t0) / span;
    return plot.left + ratio * (plot.right - plot.left);
  };
  const yFor = (frac) => plot.bottom - ((frac - yMin) / ySpan) * (plot.bottom - plot.top);
  const grouped = new Map();
  for (const point of plotted) {
    if (!grouped.has(point.sleeve)) grouped.set(point.sleeve, []);
    grouped.get(point.sleeve).push(point);
  }
  const series = [];
  for (const sleeve of CURVE_SLEEVES) {
    const points = grouped.get(sleeve);
    if (!points || !points.length) continue;
    points.sort((a, b) => a.ms - b.ms);
    series.push({
      sleeve,
      color: CURVE_COLORS[sleeve],
      points: points.map((point) => ({
        x: xFor(point.ms),
        y: yFor(point.frac),
        t: span === 0 ? 0.5 : (point.ms - t0) / span,
        asOf: point.asOf,
        frac: point.frac,
        ms: point.ms,
      })),
    });
  }
  const ticks = [];
  const tickCount = 4;
  for (let i = 0; i < tickCount; i += 1) {
    const frac = yMax - ((yMax - yMin) * i) / (tickCount - 1);
    ticks.push({ frac, y: yFor(frac) });
  }
  return { empty: false, width, height, plot, series, yMin, yMax, ticks, t0, t1 };
}

export function sampleAt(chart, ratio) {
  if (!chart || chart.empty) return null;
  const clamped = Math.min(1, Math.max(0, ratio));
  const target = chart.t0 + clamped * (chart.t1 - chart.t0);
  const values = {};
  let asOf = null;
  let best = Infinity;
  for (const series of chart.series) {
    let nearest = series.points[0];
    let dist = Infinity;
    for (const point of series.points) {
      const gap = Math.abs(point.ms - target);
      if (gap < dist) {
        dist = gap;
        nearest = point;
      }
    }
    values[series.sleeve] = nearest.frac;
    if (dist < best) {
      best = dist;
      asOf = nearest.asOf;
    }
  }
  return { asOf, values };
}
