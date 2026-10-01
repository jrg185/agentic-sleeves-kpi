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
const scorecard = read("model_scorecard.json");
const meta = read("meta.json");
const app = readFileSync(new URL("../app.js", import.meta.url), "utf8");
const html = readFileSync(new URL("../index.html", import.meta.url), "utf8");

test("crypto closed fills match sleeve win rules and the scrubbed tape", () => {
  const stats = closedFillStats(trades, "crypto");
  const fills = scorecard.closed_fills;
  assert.equal(stats.wins, fills.wins);
  assert.equal(stats.losses, fills.losses);
  assert.equal(stats.flats, fills.flats);
  assert.equal(stats.decided, fills.decided);
  assert.equal(stats.orderIdAvailable, fills.order_id_available);
  assert.equal(stats.deduped, fills.deduped);
  assert.equal(formatWinPct(stats.rate), formatWinPct(fills.win_rate));
  assert.equal(formatWinRecord(stats), formatWinRecord(fills));
  assert.match(formatWinRecord(stats), /^\d+\u2013\d+$/);
  assert.equal(
    formatPct(stats.expectancyFrac, { signed: true, digits: 2 }),
    formatPct(fills.expectancy_frac, { signed: true, digits: 2 })
  );
  assert.equal(
    formatUsd(stats.expectancyUsd, { signed: true }),
    formatUsd(fills.expectancy_usd, { signed: true })
  );
  assert.match(formatUsd(fills.expectancy_usd, { signed: true }), /^[+-]\$\d+\.\d{2}$/);
  const tapeFees = feeDragFromTrades(trades, "crypto");
  assert.equal(tapeFees.status, "known");
  assert.equal(scorecard.fee_drag.status, "known");
  assert.equal(tapeFees.n, scorecard.fee_drag.n);
  assert.equal(tapeFees.fee_usd, scorecard.fee_drag.fee_usd);
  assert.equal(tapeFees.sell_fee_usd, scorecard.fee_drag.sell_fee_usd);
  assert.equal(typeof tapeFees.fee_usd, "number");
  assert.equal(typeof tapeFees.sell_fee_usd, "number");
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
  const kill = scorecard.kill;
  for (const key of [
    "kill_headroom_stored",
    "kill_headroom_frac",
    "kill_headroom_usd",
    "day_kill_pct",
    "day_kill_usd",
    "day_target_pct",
    "day_target_usd",
  ]) {
    assert.equal(typeof kill[key], "number");
  }
  assert.equal(derived.killHeadroom, kill.kill_headroom_usd);
  assert.equal(formatUsd(derived.killHeadroom), formatUsd(kill.kill_headroom_usd));
  assert.match(formatUsd(kill.kill_headroom_usd), /^\$\d+\.\d{2}$/);
  assert.equal(formatPct(derived.killHeadroomFrac), formatPct(kill.kill_headroom_frac));
  assert.equal(derived.dayKill, kill.day_kill_usd);
  assert.equal(formatUsd(derived.dayKill), formatUsd(kill.day_kill_usd));
  assert.equal(formatPct(derived.dayKillFrac), formatPct(kill.day_kill_pct));
  assert.equal(derived.dayTarget, kill.day_target_usd);
  assert.equal(
    formatPct(derived.dayTargetFrac, { signed: true }),
    formatPct(kill.day_target_pct, { signed: true })
  );
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
  assert.match(scorecard.oos.note, /95 bps\/leg/);
  assert.match(scorecard.oos.note, /190 RT/);
  assert.match(scorecard.oos.note, /T24d will set FEE_BPS/);
  assert.equal(scorecard.oos.fee_bps, 30);
  const tapeFees = feeDragFromTrades(trades, "crypto");
  assert.equal(scorecard.fee_drag.status, "known");
  assert.equal(scorecard.fee_drag.fee_usd, tapeFees.fee_usd);
  assert.equal(scorecard.fee_drag.sell_fee_usd, tapeFees.sell_fee_usd);
  assert.equal(scorecard.fee_drag.n, tapeFees.n);
  assert.equal(scorecard.signal_linkage.status, meta.signal_linkage.status);
  assert.equal(scorecard.signal_linkage.artifact_count, meta.signal_linkage.artifact_count);
  assert.equal(scorecard.signal_linkage.outcome_count, meta.signal_linkage.outcome_count);
  assert.equal(scorecard.signal_linkage.last_generated_at, meta.signal_linkage.last_generated_at);
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
