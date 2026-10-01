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
  assert.equal(stats.wins, 23);
  assert.equal(stats.losses, 18);
  assert.equal(stats.flats, 0);
  assert.equal(stats.decided, 41);
  assert.equal(stats.orderIdAvailable, false);
  assert.equal(stats.deduped, 0);
  assert.equal(formatWinPct(stats.rate), "56%");
  assert.equal(formatWinRecord(stats), "23\u201318");
  assert.equal(formatPct(stats.expectancyFrac, { signed: true, digits: 2 }), "+0.24%");
  assert.equal(formatUsd(stats.expectancyUsd, { signed: true }), "+$0.71");
  const tapeFees = feeDragFromTrades(trades, "crypto");
  assert.equal(tapeFees.status, "known");
  assert.equal(tapeFees.n, 106);
  assert.equal(tapeFees.fee_usd, 16.64);
  assert.equal(tapeFees.sell_fee_usd, 6.53);
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
  assert.equal(scorecard.fee_drag.fee_usd, 16.64);
  assert.equal(scorecard.fee_drag.n, 106);
  assert.equal(scorecard.signal_linkage.status, "known");
  assert.equal(scorecard.signal_linkage.artifact_count, 8);
  assert.equal(scorecard.signal_linkage.outcome_count, 0);
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
