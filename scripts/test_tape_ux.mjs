import assert from "node:assert/strict";
import test from "node:test";

import {
  agentUrl,
  csvFilename,
  linkSegments,
  preferredWhy,
  pullUrl,
  tapeCsv,
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
