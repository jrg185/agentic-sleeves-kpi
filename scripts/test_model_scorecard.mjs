import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import {
  closedFillStats,
  cryptoOosModels,
  feeDragFromTrades,
  formatPct,
  deriveSleeve,
  formatUsd,
  formatWinPct,
  formatWinRecord,
  inferLiveBackend,
} from "../derive.js";

const read = (name) => JSON.parse(readFileSync(new URL(`../data/${name}`, import.meta.url), "utf8"));
const trades = read("kpi_trades_scrubbed.json");
const models = read("models.json");
const oos = read("models_oos.json");
const summary = read("kpi_summary.json");
const app = readFileSync(new URL("../app.js", import.meta.url), "utf8");
const html = readFileSync(new URL("../index.html", import.meta.url), "utf8");

test("crypto closed fills match sleeve win rules and the scrubbed tape", () => {
  const stats = closedFillStats(trades, "crypto");
  const fills = read("model_scorecard.json").closed_fills;
  for (const key of ["wins", "losses", "flats", "decided", "deduped"]) {
    assert.equal(Number.isInteger(stats[key]), true);
    assert.equal(stats[key] >= 0, true);
    assert.equal(stats[key], fills[key]);
  }
  assert.equal(stats.wins + stats.losses, stats.decided);
  assert.equal(stats.orderIdAvailable, false);
  assert.equal(fills.order_id_available, false);
  assert.equal(fills.dedupe, "scrubbed-rows");
  assert.equal(typeof stats.expectancyUsd, "number");
  assert.equal(Number.isFinite(stats.expectancyUsd), true);
  assert.equal(stats.expectancyUsd, fills.expectancy_usd);
  assert.equal(formatWinPct(stats.rate), formatWinPct(fills.win_rate));
  assert.equal(formatWinRecord(stats), formatWinRecord(fills));
  assert.equal(
    formatPct(stats.expectancyFrac, { signed: true, digits: 2 }),
    formatPct(fills.expectancy_frac, { signed: true, digits: 2 })
  );
  assert.equal(formatUsd(stats.expectancyUsd, { signed: true }), formatUsd(fills.expectancy_usd, { signed: true }));
  const tapeFees = feeDragFromTrades(trades, "crypto");
  const fees = read("model_scorecard.json").fee_drag;
  assert.equal(tapeFees.status, "known");
  assert.equal(fees.status, "known");
  for (const key of ["n", "fee_usd", "sell_fee_usd"]) {
    assert.equal(typeof tapeFees[key], "number");
    assert.equal(Number.isFinite(tapeFees[key]), true);
    assert.equal(tapeFees[key] >= 0, true);
    assert.equal(tapeFees[key], fees[key]);
  }
  assert.equal(typeof tapeFees.fee_frac, "number");
  assert.equal(Number.isFinite(tapeFees.fee_frac), true);
  assert.equal(tapeFees.fee_frac >= 0, true);
});

test("order id collapses duplicate sells and fee dollars stay explicit", () => {
  const stats = closedFillStats(
    [
      { sleeve: "crypto", side: "sell", pnl_frac_of_book: 0.01, order_id: "a" },
      { sleeve: "crypto", side: "sell", pnl_frac_of_book: 0.01, order_id: "a" },
      { sleeve: "crypto", side: "sell", pnl_frac_of_book: 0, order_id: "b" },
      { sleeve: "crypto", side: "buy", pnl_frac_of_book: 0.4, fee_usd: 0.25 },
      { sleeve: "crypto", side: "sell", pnl_frac_of_book: -0.02, fee_usd: 0.1 },
    ],
    "crypto"
  );
  assert.equal(stats.wins, 1);
  assert.equal(stats.losses, 1);
  assert.equal(stats.flats, 1);
  assert.equal(stats.deduped, 1);
  assert.equal(stats.orderIdAvailable, true);
  const fees = feeDragFromTrades(
    [
      { sleeve: "crypto", side: "buy", fee_usd: 0.25, order_id: "a" },
      { sleeve: "crypto", side: "buy", fee_usd: 0.25, order_id: "a" },
      { sleeve: "crypto", side: "sell", fee_usd: 0.1 },
    ],
    "crypto"
  );
  assert.equal(fees.status, "known");
  assert.equal(fees.fee_usd, 0.35);
  assert.equal(fees.sell_fee_usd, 0.1);
});

test("live backend stays rules and crypto OOS keeps rules, logistic, and lgbm", () => {
  const backend = inferLiveBackend(models);
  assert.equal(backend.id, "rules");
  assert.equal(backend.cli, "--backend rules");
  const rows = cryptoOosModels(oos);
  assert.deepEqual(
    rows.map((row) => row.model),
    ["rules", "logistic", "lgbm"]
  );
  assert.equal(rows.find((row) => row.model === "lgbm").promoted, true);
  assert.equal(rows.find((row) => row.model === "logistic").promoted, false);
  const crypto = summary.find((row) => row.sleeve === "crypto");
  const derived = deriveSleeve(crypto);
  const scorecard = read("model_scorecard.json");
  assert.equal(formatPct(derived.killHeadroomFrac), formatPct(scorecard.kill.kill_headroom_frac));
  assert.equal(formatUsd(derived.killHeadroom), formatUsd(scorecard.kill.kill_headroom_usd));
  assert.equal(Number.isFinite(scorecard.kill.kill_headroom_usd), true);
  assert.equal(formatPct(derived.dayKillFrac), "-10.0%");
  assert.equal(formatUsd(derived.dayKill), "-$30.00");
  assert.equal(formatPct(derived.dayTargetFrac, { signed: true }), "+2.5%");
});

test("models tab markup loads the scorecard instead of a second page", () => {
  assert.equal(html.includes('id="tab-models"'), true);
  assert.equal(html.includes("Crypto scorecard"), true);
  assert.equal(html.includes('id="panel-models"'), true);
  assert.equal(app.includes('loadJson("data/model_scorecard.json"'), true);
  assert.equal(app.includes("renderCryptoScorecard"), true);
  assert.equal(app.includes('metric("Fee drag"'), true);
  assert.equal(app.includes("UNKNOWN"), true);
  assert.equal(app.includes("30 bp"), true);
  assert.equal(app.includes("95 bps/leg"), true);
  assert.equal(app.includes("190 RT"), true);
  assert.equal(app.includes("fee_frac_of_book"), true);
  assert.equal(app.includes('"Fee"'), true);
  const scorecard = read("model_scorecard.json");
  assert.match(scorecard.oos.note, /95 bps\/leg/);
  assert.match(scorecard.oos.note, /190 RT/);
  assert.match(scorecard.oos.note, /T24d will set FEE_BPS/);
  assert.equal(scorecard.oos.fee_bps, 30);
  assert.equal(scorecard.fee_drag.status, "known");
  for (const key of ["fee_usd", "sell_fee_usd", "fee_frac"]) {
    assert.equal(typeof scorecard.fee_drag[key], "number");
    assert.equal(Number.isFinite(scorecard.fee_drag[key]), true);
    assert.equal(scorecard.fee_drag[key] >= 0, true);
  }
  assert.equal(Number.isInteger(scorecard.fee_drag.n), true);
  assert.equal(scorecard.fee_drag.n >= 0, true);
  assert.match(scorecard.fee_drag.note, /Measured live fee/);
  assert.match(scorecard.fee_drag.note, /T24d/);
  assert.equal(scorecard.signal_linkage.status, "known");
  for (const key of ["artifact_count", "outcome_count"]) {
    assert.equal(Number.isInteger(scorecard.signal_linkage[key]), true);
    assert.equal(scorecard.signal_linkage[key] >= 0, true);
  }
  assert.match(String(scorecard.signal_linkage.last_generated_at || ""), /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}/);
  assert.equal(app.includes("T24b will improve joined-fill metrics."), true);
  assert.equal(app.includes("signal_artifacts"), true);
  assert.equal(app.includes("signal_trade_outcomes"), true);
  assert.equal(app.includes("Equity modeling and place are not live."), true);
  assert.equal(app.includes("Queued RTH"), true);
  assert.equal(html.includes("Crypto owns the Agentic book."), true);
  assert.equal(html.includes("Equity research"), true);
  assert.equal(models.models.find((row) => row.sleeve === "equities").used.includes("Paused."), true);
  assert.equal(app.includes("--backend rules"), true);
});
