import assert from "node:assert/strict";
import test from "node:test";

import { readFileSync } from "node:fs";

import {
  TAPE_MONTH_ALL,
  agentUrl,
  csvFilename,
  etMonthKey,
  etMonthLabel,
  fillCountText,
  fillStamp,
  filterTapeByMonth,
  linkSegments,
  preferredWhy,
  pullUrl,
  rememberTapeMonth,
  storedTapeMonth,
  tapeCsv,
  tapeMonthOptions,
  tapeMonthStorageKey,
  tapeOpenKey,
} from "../tape.js";

const SYNC = "RH Agentic sync order 6ab7f4a2-5444-4593-84ea-e78f57dc0cf6";
const BACKFILL = "RH Agentic backfill order 6ab7f4a2-5444-4593-84ea-e78f57dc0cf6";

test("notes win when they are a human sentence", () => {
  const pref = preferredWhy({
    why: SYNC,
    notes: "First live fill. See PR#12.",
  });
  assert.equal(pref.kind, "human");
  assert.equal(pref.text, "First live fill. See PR#12.");
  assert.equal(pref.uuid, "");
});

test("human notes win over a different human why", () => {
  const pref = preferredWhy({
    why: "SWING unlock Joe/Wags; soft tgt flexible",
    notes: "First live fill.",
  });
  assert.equal(pref.text, "First live fill.");
});

test("why is used when notes are missing or only a machine id", () => {
  assert.equal(preferredWhy({ why: "+15% scale" }).text, "+15% scale");
  assert.equal(preferredWhy({ why: "+15% scale", notes: "  " }).text, "+15% scale");
  assert.equal(
    preferredWhy({ why: "artifact buy", notes: SYNC }).text,
    "artifact buy"
  );
});

test("a bare sync or backfill order is a short label plus uuid", () => {
  const sync = preferredWhy({ why: SYNC });
  assert.equal(sync.kind, "machine");
  assert.equal(sync.text, "sync order");
  assert.equal(sync.uuid, "6ab7f4a2-5444-4593-84ea-e78f57dc0cf6");
  assert.notEqual(sync.text, SYNC);

  const backfill = preferredWhy({ why: BACKFILL });
  assert.equal(backfill.text, "backfill order");
  assert.equal(backfill.uuid, "6ab7f4a2-5444-4593-84ea-e78f57dc0cf6");
});

test("a machine id with extra words stays human text", () => {
  const text = `${SYNC} after a manual add`;
  const pref = preferredWhy({ why: text });
  assert.equal(pref.kind, "human");
  assert.equal(pref.text, text);
});

test("PR and bc tokens become sleeve-aware links and leave the rest as text", () => {
  const text = "profit-exit trail breach (bc-b76ce034 / PR#50); full exit not half";
  const parts = linkSegments(text, "crypto");
  assert.deepEqual(
    parts.map((part) => part.text),
    ["profit-exit trail breach (", "bc-b76ce034", " / ", "PR#50", "); full exit not half"]
  );
  assert.equal(parts[1].href, "https://cursor.com/agents/bc-b76ce034");
  assert.equal(parts[3].href, "https://github.com/jrg185/agentic-crypto-signals/pull/50");

  const equity = linkSegments("Added on PR #7 after review", "equities");
  assert.equal(equity[1].text, "PR #7");
  assert.equal(equity[1].href, "https://github.com/jrg185/agentic-equity-signals/pull/7");
  assert.equal(pullUrl("equity", "7"), equity[1].href);
  assert.equal(agentUrl("B76CE034"), "https://cursor.com/agents/bc-b76ce034");
});

test("untrusted text is not turned into a url", () => {
  const parts = linkSegments("<img src=x onerror=alert(1)> javascript:alert(1)", "crypto");
  assert.equal(parts.length, 1);
  assert.equal(parts[0].type, "text");
  assert.equal(parts[0].href, undefined);
});

test("tape open keys and csv names are per sleeve", () => {
  assert.equal(tapeOpenKey("crypto"), "the-book-tape-open:crypto");
  assert.equal(tapeOpenKey("equities"), "the-book-tape-open:equities");
  assert.notEqual(tapeOpenKey("crypto"), tapeOpenKey("equities"));
  assert.equal(csvFilename("crypto"), "the-book-crypto-tape.csv");
  assert.equal(csvFilename("equities"), "the-book-equities-tape.csv");
});

test("csv uses human-preferred why and escapes commas and quotes", () => {
  const csv = tapeCsv([
    {
      time: "Sep 28, 2:06 AM",
      ticker: "GRT",
      side: "sell",
      notional: "$31.17 (10.39%)",
      fee: "$0.29",
      tradePnl: "+$0.91",
      runningPnl: "+$31.91",
      runningBalance: "$331.91",
      why: 'trail breach (bc-b76ce034 / PR#50), "full exit"',
    },
    {
      time: "Sep 28, 3:50 AM",
      ticker: "CRV",
      side: "sell",
      notional: "$21.46 (7.15%)",
      fee: "",
      tradePnl: "-$0.91",
      runningPnl: "+$31.01",
      runningBalance: "$331.01",
      why: preferredWhy({ why: SYNC }).text,
    },
  ]);
  const lines = csv.trim().split("\r\n");
  assert.equal(
    lines[0],
    "time,ticker,side,notional,fee,trade pnl,running pnl,running balance,why"
  );
  assert.match(lines[1], /^"Sep 28, 2:06 AM",GRT,sell,\$31\.17 \(10\.39%\),\$0\.29,\+\$0\.91/);
  assert.match(lines[1], /"trail breach \(bc-b76ce034 \/ PR#50\), ""full exit"""/);
  assert.match(lines[2], /,sync order$/);
  assert.doesNotMatch(lines[2], /RH Agentic sync order/);
});

function memoryStorage(initial = {}) {
  const data = { ...initial };
  return {
    getItem(key) {
      return Object.prototype.hasOwnProperty.call(data, key) ? data[key] : null;
    },
    setItem(key, value) {
      data[key] = String(value);
    },
  };
}

test("ET month keys follow America/New_York, including the midnight boundary", () => {
  assert.equal(etMonthKey("2026-09-26T16:36:50+00:00"), "2026-09");
  assert.equal(etMonthKey("2026-10-01T03:59:59Z"), "2026-09");
  assert.equal(etMonthKey("2026-10-01T04:00:00Z"), "2026-10");
  assert.equal(etMonthKey("2026-12-01T04:30:00Z"), "2026-11");
  assert.equal(etMonthKey("not-a-time"), "");
  assert.equal(etMonthKey(""), "");
  assert.equal(etMonthLabel("2026-09"), "Sep 2026");
  assert.equal(etMonthLabel("2026-10"), "Oct 2026");
  assert.equal(etMonthLabel("2026-13"), "");
  assert.equal(etMonthLabel("all"), "");
});

test("month options are distinct ET months, oldest first, and All keeps every row", () => {
  const rows = [
    { timestamp_et: "2026-10-01T12:48:32Z", ticker: "ORCA" },
    { timestamp_et: "2026-10-01T03:48:01Z", ticker: "OP" },
    { ts: "2026-09-26T16:36:50Z", ticker: "AVAX" },
    { timestamp_et: "nope", ticker: "BAD" },
    { ticker: "NONE" },
  ];
  assert.deepEqual(tapeMonthOptions(rows), ["2026-09", "2026-10"]);
  assert.equal(etMonthKey(fillStamp(rows[1])), "2026-09");
  assert.equal(etMonthKey(fillStamp(rows[0])), "2026-10");

  const all = filterTapeByMonth(rows, TAPE_MONTH_ALL);
  assert.equal(all.length, rows.length);
  assert.notEqual(all, rows);
  assert.deepEqual(
    filterTapeByMonth(rows, "2026-09").map((row) => row.ticker),
    ["OP", "AVAX"]
  );
  assert.deepEqual(
    filterTapeByMonth(rows, "2026-10").map((row) => row.ticker),
    ["ORCA"]
  );
  assert.equal(filterTapeByMonth(rows, "2099-01").length, 0);
});

test("fill count names the visible rows and the total only when filtered", () => {
  assert.equal(fillCountText(117, 117), "117 fills");
  assert.equal(fillCountText(1, 1), "1 fill");
  assert.equal(fillCountText(0, 0), "0 fills");
  assert.equal(fillCountText(12, 117), "12 of 117 fills");
  assert.equal(fillCountText(1, 117), "1 of 117 fills");
  assert.equal(fillCountText(0, 117), "0 of 117 fills");
});

test("month filter memory is per sleeve and first visit is All", () => {
  assert.equal(tapeMonthStorageKey("crypto"), "the-book-tape-month:crypto");
  assert.equal(tapeMonthStorageKey("equities"), "the-book-tape-month:equities");
  assert.notEqual(tapeMonthStorageKey("crypto"), tapeOpenKey("crypto"));
  assert.notEqual(tapeMonthStorageKey("crypto"), tapeMonthStorageKey("equities"));

  const months = ["2026-09", "2026-10"];
  const storage = memoryStorage();
  assert.equal(storedTapeMonth(storage, tapeMonthStorageKey("crypto"), months), TAPE_MONTH_ALL);

  rememberTapeMonth(storage, tapeMonthStorageKey("crypto"), "2026-09");
  rememberTapeMonth(storage, tapeMonthStorageKey("equities"), "2026-10");
  assert.equal(storedTapeMonth(storage, tapeMonthStorageKey("crypto"), months), "2026-09");
  assert.equal(storedTapeMonth(storage, tapeMonthStorageKey("equities"), months), "2026-10");
  assert.equal(storedTapeMonth(storage, tapeMonthStorageKey("crypto"), ["2026-10"]), TAPE_MONTH_ALL);

  const broken = {
    getItem() {
      throw new Error("private");
    },
    setItem() {
      throw new Error("private");
    },
  };
  assert.equal(storedTapeMonth(broken, tapeMonthStorageKey("crypto"), months), TAPE_MONTH_ALL);
  assert.doesNotThrow(() => rememberTapeMonth(broken, tapeMonthStorageKey("crypto"), "2026-09"));
});

test("scrubbed crypto tape has September and October and All keeps both", () => {
  const trades = JSON.parse(readFileSync(new URL("../data/kpi_trades_scrubbed.json", import.meta.url), "utf8"));
  const crypto = trades.filter((row) => row.sleeve === "crypto");
  const equities = trades.filter((row) => row.sleeve === "equities");
  const cryptoMonths = tapeMonthOptions(crypto);
  assert.ok(cryptoMonths.includes("2026-09"));
  assert.ok(cryptoMonths.includes("2026-10"));
  assert.deepEqual(cryptoMonths, [...cryptoMonths].sort());

  const september = filterTapeByMonth(crypto, "2026-09");
  const october = filterTapeByMonth(crypto, "2026-10");
  assert.ok(september.length > 0);
  assert.ok(october.length > 0);
  assert.equal(september.length + october.length, crypto.length);
  assert.equal(filterTapeByMonth(crypto, TAPE_MONTH_ALL).length, crypto.length);
  assert.ok(september.every((row) => etMonthKey(fillStamp(row)) === "2026-09"));
  assert.ok(october.every((row) => etMonthKey(fillStamp(row)) !== "2026-09"));
  assert.equal(fillCountText(september.length, crypto.length), `${september.length} of ${crypto.length} fills`);
  assert.equal(fillCountText(crypto.length, crypto.length), `${crypto.length} fills`);

  assert.equal(filterTapeByMonth(equities, TAPE_MONTH_ALL).length, equities.length);
  const equityMonths = tapeMonthOptions(equities);
  const equitySum = equityMonths.reduce((sum, key) => sum + filterTapeByMonth(equities, key).length, 0);
  assert.equal(equitySum, equities.length);
});

test("desktop fixed tape leaves Why a real share of the table", () => {
  const css = readFileSync(new URL("../styles.css", import.meta.url), "utf8");
  const start = css.indexOf("@media (min-width: 800px)");
  const end = css.indexOf("@media (max-width: 860px)");
  assert.ok(start >= 0 && end > start, "desktop tape media query");
  const block = css.slice(start, end);
  assert.match(block, /table-layout:\s*fixed/);
  const widths = new Map();
  for (const match of block.matchAll(/\.tape th:nth-child\((\d+)\)\s*\{\s*width:\s*(\d+(?:\.\d+)?)%;/g)) {
    widths.set(Number(match[1]), Number(match[2]));
  }
  assert.deepEqual([...widths.keys()], [1, 2, 3, 4, 5, 6, 7, 8, 9]);
  const sum = [...widths.values()].reduce((total, width) => total + width, 0);
  assert.equal(sum, 100);
  const firstEight = [1, 2, 3, 4, 5, 6, 7, 8].reduce((total, index) => total + widths.get(index), 0);
  assert.ok(firstEight <= 85, `columns 1–8 took ${firstEight}%`);
  assert.ok(widths.get(9) >= 15, `Why column is ${widths.get(9)}%`);
});
