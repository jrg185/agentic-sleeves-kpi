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
      row.tradePnl,
      row.runningPnl,
      row.runningBalance,
      row.why,
    ];
    lines.push(cells.map(csvEscape).join(","));
  }
  return `${lines.join("\r\n")}\r\n`;
}
