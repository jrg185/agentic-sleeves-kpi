import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import { cardOpenKey, defaultCardOpen, modelCardId, rememberOpen, storedOpen } from "../collapse.js";
import { buildChart, filterRows, sampleAt } from "../curves.js";
import {
  formatWinPct,
  formatWinRecord,
  winStats,
  winTone,
} from "../derive.js";
import { posOpenKey, positionsFor, positionRows, sortPositions } from "../positions.js";
import { pullUrl, tapeOpenKey } from "../tape.js";

const html = readFileSync(new URL("../index.html", import.meta.url), "utf8");

function signalHrefs(label) {
  const re = new RegExp(`<a\\b[^>]*>${label}</a>`, "g");
  const hrefs = [];
  for (const match of html.match(re) || []) {
    const href = match.match(/href="([^"]+)"/);
    hrefs.push(href ? href[1] : "");
  }
  return hrefs;
}

test("header and footer signal links stay on this page", () => {
  for (const label of ["Crypto signals", "Equity research"]) {
    const hrefs = signalHrefs(label);
    assert.equal(hrefs.length, 2, label);
    for (const href of hrefs) {
      assert.equal(href.startsWith("#model-"), true, href);
      assert.equal(href.includes("github.com"), false, href);
      assert.equal(/agentic-(crypto|equity)-signals/.test(href), false, href);
    }
  }
  assert.deepEqual(signalHrefs("Crypto signals"), ["#model-crypto", "#model-crypto"]);
  assert.deepEqual(signalHrefs("Equity research"), ["#model-equities", "#model-equities"]);
  assert.equal(html.includes('href="https://github.com/jrg185/the-book"'), true);
});

test("why-text PR links still target the signal repos for an authenticated reader", () => {
  assert.equal(pullUrl("crypto", "50"), "https://github.com/jrg185/agentic-crypto-signals/pull/50");
  assert.equal(pullUrl("equities", "7"), "https://github.com/jrg185/agentic-equity-signals/pull/7");
});

test("curve geometry uses fractions and does not invent a point", () => {
  const rows = [
    { sleeve: "crypto", as_of: "2026-09-27T00:00:00Z", running_balance_frac: 1.0, running_balance_usd: 323.77, account_id: "546048042" },
    { sleeve: "crypto", as_of: "2026-09-28T00:00:00Z", running_balance_frac: 1.05 },
    { sleeve: "equities", as_of: "2026-09-28T00:00:00Z", running_balance_frac: 0.99 },
    { sleeve: "combined", as_of: "2026-09-28T00:00:00Z", running_balance_frac: 1.02 },
    { sleeve: "crypto", as_of: "2026-09-29T00:00:00Z" },
  ];
  const chart = buildChart(rows, "all");
  assert.equal(chart.empty, false);
  const crypto = chart.series.find((series) => series.sleeve === "crypto");
  assert.equal(crypto.points.length, 2);
  assert.ok(crypto.points[1].x > crypto.points[0].x);
  assert.ok(crypto.points[1].y < crypto.points[0].y);
  assert.equal(filterRows(rows, "equities").length, 1);
  const only = buildChart(rows, "equities");
  assert.equal(only.series.length, 1);
  assert.equal(only.series[0].sleeve, "equities");
  const sampled = sampleAt(chart, 1);
  assert.equal(sampled.values.crypto, 1.05);
  assert.equal(sampled.values.equities, 0.99);
  const blob = JSON.stringify(chart);
  assert.equal(blob.includes("running_balance_usd"), false);
  assert.equal(blob.includes("546048042"), false);
  assert.equal(blob.includes("323.77"), false);
  assert.equal(buildChart([], "all").empty, true);
  assert.equal(buildChart([{ sleeve: "crypto", as_of: "2026-09-28T00:00:00Z" }], "crypto").empty, true);
});

test("positions group by sleeve and a flat sleeve stays empty", () => {
  const rows = sortPositions(
    positionRows({
      positions: [
        { sleeve: "equities", ticker: "QCOM", side: "long", qty: "2", unrealized_pnl_frac: 0.04 },
        { sleeve: "crypto", ticker: "AAA", side: "long", qty: "15", unrealized_pnl_frac: 0.05 },
        { sleeve: "nope", ticker: "SECRET", qty: "1" },
      ],
    })
  );
  assert.deepEqual(
    rows.map((row) => row.ticker),
    ["AAA", "QCOM"]
  );
  assert.equal(positionsFor(rows, "crypto").length, 1);
  assert.equal(positionsFor(rows, "equities")[0].ticker, "QCOM");
  assert.equal(positionsFor(rows, "combined").length, 2);
  assert.equal(positionsFor([], "equities").length, 0);
});

test("win % counts sell exits, drops flats, and combines sleeves", () => {
  const trades = [
    { sleeve: "crypto", side: "buy", pnl_frac_of_book: 0.2 },
    { sleeve: "crypto", side: "SELL", pnl_frac_of_book: 0.01 },
    { sleeve: "Crypto", side: "sell", pnl_frac: -0.02 },
    { sleeve: "crypto", side: "sell", pnl_frac_of_book: 0, pnl_frac: 0.5 },
    { sleeve: "crypto", side: "sell", pnl_frac_of_book: "nope", pnl_frac: 0.03 },
    { sleeve: "crypto", side: "sell", pnl_frac_of_book: null },
    { sleeve: "crypto", side: "sell", pnl_frac_of_book: Number.POSITIVE_INFINITY },
    { sleeve: "crypto", side: "sell", pnl_frac_of_book: 0.04 },
    { sleeve: "equities", side: "sell", pnl_frac_of_book: -0.05 },
    { sleeve: "equities", side: "buy", pnl_frac_of_book: 0.9 },
    { sleeve: "equities", side: "sell", pnl_frac_of_book: 0 },
    { sleeve: "other", side: "sell", pnl_frac_of_book: 0.5 },
  ];

  const crypto = winStats(trades, "crypto");
  assert.deepEqual(crypto, { wins: 3, losses: 1, rate: 0.75 });
  assert.equal(formatWinPct(crypto.rate), "75%");
  assert.equal(formatWinRecord(crypto), "3\u20131");
  assert.equal(winTone(crypto), "up");

  const equities = winStats(trades, "equities");
  assert.deepEqual(equities, { wins: 0, losses: 1, rate: 0 });
  assert.equal(formatWinPct(equities.rate), "0%");
  assert.equal(formatWinRecord(equities), "0\u20131");
  assert.equal(winTone(equities), "down");

  const combined = winStats(trades, "combined");
  assert.equal(combined.wins, crypto.wins + equities.wins);
  assert.equal(combined.losses, crypto.losses + equities.losses);
  assert.equal(combined.rate, 3 / 5);
  assert.equal(formatWinPct(combined.rate), "60%");
  assert.equal(formatWinRecord(combined), "3\u20132");
  assert.equal(winTone(combined), "up");

  const even = winStats(
    [
      { sleeve: "crypto", side: "sell", pnl_frac_of_book: 0.1 },
      { sleeve: "crypto", side: "sell", pnl_frac_of_book: -0.1 },
      { sleeve: "crypto", side: "sell", pnl_frac_of_book: 0 },
    ],
    "crypto"
  );
  assert.equal(even.rate, 0.5);
  assert.equal(formatWinPct(even.rate), "50%");
  assert.equal(winTone(even), "flat");

  const none = winStats([{ sleeve: "crypto", side: "buy", pnl_frac_of_book: 0.2 }], "crypto");
  assert.deepEqual(none, { wins: 0, losses: 0, rate: null });
  assert.equal(formatWinPct(none.rate), "\u2014");
  assert.equal(formatWinRecord(none), "0\u20130");
  assert.equal(winTone(none), "flat");
  assert.equal(winTone(null), "flat");
});

test("scrubbed tape yields a win % for each sleeve", () => {
  const trades = JSON.parse(readFileSync(new URL("../data/kpi_trades_scrubbed.json", import.meta.url), "utf8"));
  const crypto = winStats(trades, "crypto");
  const equities = winStats(trades, "equities");
  const combined = winStats(trades, "combined");
  assert.equal(combined.wins, crypto.wins + equities.wins);
  assert.equal(combined.losses, crypto.losses + equities.losses);
  for (const stats of [crypto, equities, combined]) {
    const label = formatWinPct(stats.rate);
    assert.match(label, /^(?:\u2014|\d+%)$/);
    assert.match(formatWinRecord(stats), /^\d+\u2013\d+$/);
    if (stats.rate == null) assert.equal(winTone(stats), "flat");
    else if (stats.rate > 0.5) assert.equal(winTone(stats), "up");
    else if (stats.rate < 0.5) assert.equal(winTone(stats), "down");
    else assert.equal(winTone(stats), "flat");
  }
});

test("card collapse defaults remember a toggle and stay off tape and position keys", () => {
  assert.equal(defaultCardOpen("equities"), false);
  assert.equal(defaultCardOpen("Equities"), false);
  assert.equal(defaultCardOpen("equity"), false);
  assert.equal(defaultCardOpen("crypto"), true);
  assert.equal(defaultCardOpen("combined"), true);
  assert.equal(defaultCardOpen(""), true);
  assert.equal(cardOpenKey("equities"), "the-book-card-open:equities");
  assert.equal(cardOpenKey("crypto"), "the-book-card-open:crypto");
  assert.equal(cardOpenKey("combined"), "the-book-card-open:combined");
  assert.equal(cardOpenKey("crypto-scorecard"), "the-book-card-open:crypto-scorecard");
  assert.notEqual(cardOpenKey("crypto"), tapeOpenKey("crypto"));
  assert.notEqual(cardOpenKey("equities"), posOpenKey("equities"));
  assert.equal(modelCardId({ sleeve: "equities", name: "lgbm_equity_v1" }), "model-equities-lgbm-equity-v1");
  assert.equal(modelCardId({ sleeve: "crypto", name: "Crypto lgbm_v1" }), "model-crypto-crypto-lgbm-v1");

  const mem = new Map();
  const storage = {
    getItem: (key) => (mem.has(key) ? mem.get(key) : null),
    setItem: (key, value) => mem.set(key, String(value)),
  };
  assert.equal(storedOpen(storage, cardOpenKey("equities"), defaultCardOpen("equities")), false);
  assert.equal(storedOpen(storage, cardOpenKey("crypto"), defaultCardOpen("crypto")), true);
  assert.equal(storedOpen(storage, cardOpenKey("combined"), defaultCardOpen("combined")), true);
  assert.equal(storedOpen(storage, tapeOpenKey("equities"), true), true);
  assert.equal(storedOpen(storage, posOpenKey("equities"), true), true);
  rememberOpen(storage, cardOpenKey("equities"), true);
  assert.equal(storedOpen(storage, cardOpenKey("equities"), false), true);
  rememberOpen(storage, cardOpenKey("crypto"), false);
  assert.equal(storedOpen(storage, cardOpenKey("crypto"), true), false);
  mem.set(cardOpenKey("combined"), "nope");
  assert.equal(storedOpen(storage, cardOpenKey("combined"), true), true);
  const broken = {
    getItem() {
      throw new Error("private");
    },
    setItem() {
      throw new Error("quota");
    },
  };
  assert.equal(storedOpen(broken, cardOpenKey("equities"), false), false);
  assert.equal(storedOpen(broken, tapeOpenKey("crypto"), true), true);
  assert.doesNotThrow(() => rememberOpen(broken, cardOpenKey("crypto"), false));
});

test("position open keys are per sleeve and do not reuse tape keys", () => {
  assert.equal(posOpenKey("combined"), "the-book-pos-open:combined");
  assert.equal(posOpenKey("crypto"), "the-book-pos-open:crypto");
  assert.equal(posOpenKey("equities"), "the-book-pos-open:equities");
  assert.notEqual(posOpenKey("crypto"), posOpenKey("equities"));
  assert.notEqual(posOpenKey("crypto"), tapeOpenKey("crypto"));
  assert.notEqual(posOpenKey("equities"), tapeOpenKey("equities"));
});
