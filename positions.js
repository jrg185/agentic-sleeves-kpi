// Open-position grouping. Dollars stay seed × fraction in the page, not in this file.

export function normalizeSleeve(value) {
  const key = String(value || "").trim().toLowerCase();
  if (key === "equity") return "equities";
  return key === "crypto" || key === "equities" ? key : "";
}

export function positionRows(payload) {
  const rows = Array.isArray(payload) ? payload : payload?.positions;
  if (!Array.isArray(rows)) return [];
  return rows.filter((row) => row && typeof row === "object" && row.ticker && normalizeSleeve(row.sleeve));
}

export function positionsFor(rows, sleeve) {
  const list = Array.isArray(rows) ? rows : [];
  if (sleeve === "combined") {
    return list.filter((row) => normalizeSleeve(row.sleeve));
  }
  const wanted = normalizeSleeve(sleeve);
  return list.filter((row) => normalizeSleeve(row.sleeve) === wanted);
}

export function posOpenKey(sleeve) {
  return `the-book-pos-open:${sleeve}`;
}

const SLEEVE_ORDER = { crypto: 0, equities: 1 };

export function sortPositions(rows) {
  return rows.slice().sort((a, b) => {
    const as = SLEEVE_ORDER[normalizeSleeve(a.sleeve)] ?? 9;
    const bs = SLEEVE_ORDER[normalizeSleeve(b.sleeve)] ?? 9;
    if (as !== bs) return as - bs;
    return String(a.ticker).localeCompare(String(b.ticker));
  });
}
