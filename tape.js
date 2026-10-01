// Tape display helpers. No DOM, no network. The page still reads scrubbed JSON only.

export const REPOS = {
  book: "https://github.com/jrg185/the-book",
  crypto: "https://github.com/jrg185/agentic-crypto-signals",
  equities: "https://github.com/jrg185/agentic-equity-signals",
};

const MACHINE_WHY_RE =
  /^RH Agentic (backfill|sync) order ([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})$/;

const LINK_RE = /\bPR ?#(\d+)|\bbc-([0-9a-fA-F]{4,})/gi;

export const TAPE_CSV_HEADER = [
  "time",
  "ticker",
  "side",
  "notional",
  "fee",
  "trade pnl",
  "running pnl",
  "running balance",
  "why",
];

function textOf(value) {
  if (value == null) return "";
  return String(value).trim();
}

export function parseMachineWhy(value) {
  const match = MACHINE_WHY_RE.exec(textOf(value));
  if (!match) return null;
  return { kind: match[1].toLowerCase(), uuid: match[2] };
}

export function isHumanText(value) {
  const text = textOf(value);
  return Boolean(text) && !parseMachineWhy(text);
}

export function machineLabel(kind) {
  return kind === "backfill" ? "backfill order" : "sync order";
}

export function preferredWhy(trade) {
  const notes = textOf(trade?.notes);
  const why = textOf(trade?.why);
  if (isHumanText(notes)) return { kind: "human", text: notes, uuid: "" };
  if (isHumanText(why)) return { kind: "human", text: why, uuid: "" };
  const machine = parseMachineWhy(why) || parseMachineWhy(notes);
  if (machine) {
    return { kind: "machine", text: machineLabel(machine.kind), uuid: machine.uuid };
  }
  return { kind: "empty", text: "", uuid: "" };
}

export function signalsRepo(sleeve) {
  const key = String(sleeve || "").trim().toLowerCase();
  if (key === "equities" || key === "equity") return REPOS.equities;
  return REPOS.crypto;
}

export function pullUrl(sleeve, number) {
  return `${signalsRepo(sleeve)}/pull/${number}`;
}

export function agentUrl(hex) {
  return `https://cursor.com/agents/bc-${String(hex).toLowerCase()}`;
}

export function linkSegments(text, sleeve) {
  const source = text == null ? "" : String(text);
  const segments = [];
  const re = new RegExp(LINK_RE.source, LINK_RE.flags);
  let last = 0;
  let match;
  while ((match = re.exec(source)) !== null) {
    if (match.index > last) {
      segments.push({ type: "text", text: source.slice(last, match.index) });
    }
    if (match[1] != null) {
      segments.push({ type: "link", text: match[0], href: pullUrl(sleeve, match[1]) });
    } else {
      segments.push({ type: "link", text: match[0], href: agentUrl(match[2]) });
    }
    last = match.index + match[0].length;
  }
  if (last < source.length) segments.push({ type: "text", text: source.slice(last) });
  return segments;
}

export function tapeOpenKey(sleeve) {
  return `the-book-tape-open:${sleeve}`;
}

export const TAPE_MONTH_ALL = "all";

const MONTH_SHORT = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

export function fillStamp(trade) {
  return trade?.timestamp_et || trade?.ts || "";
}

export function etMonthKey(value) {
  const date = value instanceof Date ? value : new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone: "America/New_York",
    year: "numeric",
    month: "2-digit",
  }).formatToParts(date);
  const year = parts.find((part) => part.type === "year")?.value;
  const month = parts.find((part) => part.type === "month")?.value;
  if (!year || !month) return "";
  return `${year}-${month}`;
}

export function etMonthLabel(key) {
  const match = /^(\d{4})-(\d{2})$/.exec(String(key || ""));
  if (!match) return "";
  const index = Number(match[2]) - 1;
  if (index < 0 || index > 11) return "";
  return `${MONTH_SHORT[index]} ${match[1]}`;
}

export function tapeMonthOptions(rows, stampOf = fillStamp) {
  const keys = new Set();
  for (const row of rows || []) {
    const key = etMonthKey(stampOf(row));
    if (key) keys.add(key);
  }
  return [...keys].sort();
}

export function filterTapeByMonth(rows, monthKey, stampOf = fillStamp) {
  const list = Array.isArray(rows) ? rows : [];
  if (!monthKey || monthKey === TAPE_MONTH_ALL) return list.slice();
  return list.filter((row) => etMonthKey(stampOf(row)) === monthKey);
}

export function fillCountText(visible, total) {
  const shown = Math.max(0, Number(visible) || 0);
  const all = Math.max(0, Number(total) || 0);
  if (shown === all) return all === 1 ? "1 fill" : `${all} fills`;
  const noun = all === 1 ? "fill" : "fills";
  return `${shown} of ${all} ${noun}`;
}

export function tapeMonthStorageKey(sleeve) {
  return `the-book-tape-month:${sleeve}`;
}

export function storedTapeMonth(storage, key, options) {
  const allowed = new Set([TAPE_MONTH_ALL, ...(options || [])]);
  try {
    const value = storage.getItem(key);
    if (allowed.has(value)) return value;
  } catch {
    /* localStorage can throw in private mode. */
  }
  return TAPE_MONTH_ALL;
}

export function rememberTapeMonth(storage, key, value) {
  try {
    storage.setItem(key, value || TAPE_MONTH_ALL);
  } catch {
    /* Ignore quota and private-mode failures. The filter still works this visit. */
  }
}

export function csvFilename(sleeve) {
  const key = String(sleeve || "book")
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-|-$/g, "");
  return `the-book-${key || "book"}-tape.csv`;
}

export function csvEscape(value) {
  const text = value == null ? "" : String(value);
  if (/[",\r\n]/.test(text)) return `"${text.replaceAll('"', '""')}"`;
  return text;
}

export function tapeCsv(rows) {
  const lines = [TAPE_CSV_HEADER.join(",")];
  for (const row of rows) {
    const cells = [
      row.time,
      row.ticker,
      row.side,
      row.notional,
      row.fee,
      row.tradePnl,
      row.runningPnl,
      row.runningBalance,
      row.why,
    ];
    lines.push(cells.map(csvEscape).join(","));
  }
  return `${lines.join("\r\n")}\r\n`;
}
