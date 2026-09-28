import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import { buildChart, filterRows, sampleAt } from "../curves.js";
import { positionsFor, positionRows, sortPositions } from "../positions.js";
import { pullUrl } from "../tape.js";

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
  for (const label of ["Crypto signals", "Equity signals"]) {
    const hrefs = signalHrefs(label);
    assert.equal(hrefs.length, 2, label);
    for (const href of hrefs) {
      assert.equal(href.startsWith("#model-"), true, href);
      assert.equal(href.includes("github.com"), false, href);
      assert.equal(/agentic-(crypto|equity)-signals/.test(href), false, href);
    }
  }
  assert.deepEqual(signalHrefs("Crypto signals"), ["#model-crypto", "#model-crypto"]);
  assert.deepEqual(signalHrefs("Equity signals"), ["#model-equities", "#model-equities"]);
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
