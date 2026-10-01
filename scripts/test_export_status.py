"""Failure stamps stay on meta.json and do not rewrite KPI numbers."""

import json
import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import export_kpi


class ExportStatusTests(unittest.TestCase):
    def test_stamp_keeps_kpi_numbers_and_last_fetch(self):
        with TemporaryDirectory() as tmp:
            target = Path(tmp)
            summary = [{"sleeve": "crypto", "as_of": "2026-09-27T23:48:06+00:00", "running_pnl_frac": 0.079233}]
            export_kpi.write_json(target / "kpi_summary.json", summary)
            export_kpi.write_json(
                target / "meta.json",
                {
                    "source": "supabase",
                    "fetched_at": "2026-09-28T01:08:27Z",
                    "row_counts": {"kpi_summary": 3},
                },
            )
            export_kpi.stamp_export_failure(
                target,
                "error",
                "Export failed: disk full. KPI numbers were left unchanged.",
            )
            kpi = json.loads((target / "kpi_summary.json").read_text(encoding="utf-8"))
            meta = json.loads((target / "meta.json").read_text(encoding="utf-8"))
        self.assertEqual(kpi, summary)
        self.assertEqual(meta["fetched_at"], "2026-09-28T01:08:27Z")
        self.assertEqual(meta["export_status"], "error")
        self.assertEqual(meta["row_counts"]["kpi_summary"], 3)
        self.assertIn("disk full", meta["export_error"])
        self.assertTrue(meta["export_attempted_at"])

    def test_failure_messages_do_not_invent_numbers_or_keep_secrets(self):
        status, message = export_kpi.failure_message(OSError(28, "No space left on device"))
        self.assertEqual(status, "error")
        self.assertIn("disk full", message)
        self.assertNotIn("0.079233", message)

        status, message = export_kpi.failure_message(OSError(30, "Read-only file system"))
        self.assertEqual(status, "error")
        self.assertIn("read-only", message)

        status, message = export_kpi.failure_message(RuntimeError("cannot execute INSERT in a read-only transaction"))
        self.assertIn("read-only", message)

        leaked = "eyJhbGciOiJIUzI1NiJ9.payload.signature"
        status, message = export_kpi.failure_message(RuntimeError(f"REST kpi_summary HTTP 401: {leaked}"))
        self.assertEqual(status, "error")
        self.assertNotIn(leaked, message)
        self.assertNotIn("eyJ", message)
        self.assertIn("KPI numbers were left unchanged", message)

    def test_read_only_and_disk_full_name_the_frozen_snapshot(self):
        frozen = "2026-09-27T23:48:06+00:00"
        status, message, warehouse = export_kpi.classify_failure(
            RuntimeError("INSERT rejected: read-only transaction (25006). kpi_sleeve_snapshots was not updated."),
            frozen,
        )
        self.assertEqual(status, "error")
        self.assertEqual(warehouse, "read-only")
        self.assertIn("warehouse read-only — snapshot frozen at", message)
        self.assertIn(frozen, message)

        status, message, warehouse = export_kpi.classify_failure(
            RuntimeError("INSERT rejected: database disk full (53100). Snapshots were not updated."),
            frozen,
        )
        self.assertEqual(warehouse, "disk-full")
        self.assertIn("warehouse disk full — snapshot frozen at", message)
        self.assertNotIn("323.77", message)

    def test_missing_credentials_stamp_meta_only(self):
        with TemporaryDirectory() as tmp:
            target = Path(tmp)
            summary = [{"sleeve": "crypto", "as_of": "2026-09-27T23:48:06+00:00"}]
            trades = [{"sleeve": "crypto", "ticker": "QNT", "pnl_frac": 0.013266}]
            for name, payload in (
                ("kpi_summary.json", summary),
                ("kpi_trades_scrubbed.json", trades),
                ("models_oos.json", {"rows": []}),
                ("models.json", {"models": []}),
                ("meta.json", {"source": "supabase", "fetched_at": "2026-09-28T01:08:27Z"}),
            ):
                export_kpi.write_json(target / name, payload)
            env = {key: value for key, value in os.environ.items() if key not in {"SUPABASE_SERVICE_ROLE_KEY", "SUPABASE_DB_URL"}}
            with mock.patch.object(export_kpi, "DATA", target), mock.patch.dict(os.environ, env, clear=True):
                code = export_kpi.main([])
            kpi = json.loads((target / "kpi_summary.json").read_text(encoding="utf-8"))
            tape = json.loads((target / "kpi_trades_scrubbed.json").read_text(encoding="utf-8"))
            meta = json.loads((target / "meta.json").read_text(encoding="utf-8"))
        self.assertEqual(code, 0)
        self.assertEqual(kpi, summary)
        self.assertEqual(tape, trades)
        self.assertEqual(meta["fetched_at"], "2026-09-28T01:08:27Z")
        self.assertEqual(meta["export_status"], "stale")
        self.assertIn("unset", meta["export_error"])


class TapeLedgerTests(unittest.TestCase):
    def test_committed_scrubbed_tape_gets_a_running_ledger(self):
        from decimal import Decimal

        path = Path(__file__).resolve().parents[1] / "data" / "kpi_trades_scrubbed.json"
        raw = json.loads(path.read_text(encoding="utf-8"))
        bare = []
        for row in raw:
            bare.append(
                {
                    key: value
                    for key, value in row.items()
                    if key not in {"running_pnl_frac", "running_balance_frac"}
                }
            )
        out = export_kpi.attach_running_ledger(bare)
        crypto = sorted(
            (row for row in out if row.get("sleeve") == "crypto"),
            key=lambda row: row["timestamp_et"],
        )
        self.assertGreater(len(crypto), 1)
        last = crypto[-1]
        self.assertIsNotNone(last["running_pnl_frac"])
        self.assertIsNotNone(last["running_balance_frac"])
        self.assertAlmostEqual(last["running_balance_frac"], 1 + last["running_pnl_frac"], places=6)
        committed = [
            row for row in raw if row.get("sleeve") == "crypto"
        ]
        committed.sort(key=lambda row: row["timestamp_et"])
        # Expected tip is the last committed crypto row by timestamp_et.
        tip = committed[-1]
        self.assertEqual(last["ticker"], tip["ticker"])
        self.assertEqual(last["side"], tip["side"])
        self.assertEqual(last["timestamp_et"], tip["timestamp_et"])
        self.assertAlmostEqual(
            last["running_pnl_frac"],
            float(export_kpi.q6(Decimal(str(tip["running_pnl_frac"])))),
            places=6,
        )
        self.assertAlmostEqual(
            last["running_balance_frac"],
            float(export_kpi.q6(Decimal(str(tip["running_balance_frac"])))),
            places=6,
        )
        self.assertEqual(tip["running_pnl_frac"], last["running_pnl_frac"])
        self.assertEqual(tip["running_balance_frac"], last["running_balance_frac"])
        long_why = "n" * 240
        kept = export_kpi.attach_running_ledger(
            [
                {
                    "sleeve": "crypto",
                    "timestamp_et": "2026-09-27T00:00:00+00:00",
                    "ticker": "W",
                    "side": "buy",
                    "pnl_frac_of_book": 0,
                    "why": long_why,
                }
            ]
        )
        self.assertEqual(kept[0]["why"], long_why)
        self.assertEqual(len(kept[0]["why"]), 240)


class ScorecardTests(unittest.TestCase):
    def test_committed_crypto_scorecard_matches_exported_json(self):
        root = Path(__file__).resolve().parents[1]
        summary = json.loads((root / "data" / "kpi_summary.json").read_text(encoding="utf-8"))
        trades = json.loads((root / "data" / "kpi_trades_scrubbed.json").read_text(encoding="utf-8"))
        models = json.loads((root / "data" / "models.json").read_text(encoding="utf-8"))
        oos = json.loads((root / "data" / "models_oos.json").read_text(encoding="utf-8"))
        meta = json.loads((root / "data" / "meta.json").read_text(encoding="utf-8"))
        built = export_kpi.build_model_scorecard(
            summary,
            trades,
            models,
            oos,
            signal_linkage=meta.get("signal_linkage"),
        )
        committed = json.loads((root / "data" / "model_scorecard.json").read_text(encoding="utf-8"))
        self.assertEqual(committed, built)
        self.assertEqual(built["live_backend"]["id"], "rules")
        self.assertEqual(built["live_backend"]["cli"], "--backend rules")
        self.assertEqual(built["live_backend"]["promoted_model"], "lgbm")
        self.assertIs(built["live_backend"]["promoted_in_use"], False)
        self.assertEqual(built["closed_fills"]["wins"], 23)
        self.assertEqual(built["closed_fills"]["losses"], 18)
        self.assertEqual(built["closed_fills"]["expectancy_usd"], 0.71)
        self.assertEqual(built["closed_fills"]["dedupe"], "scrubbed-rows")
        self.assertEqual(built["fee_drag"]["status"], "known")
        self.assertEqual(built["fee_drag"]["fee_usd"], 16.64)
        self.assertEqual(built["fee_drag"]["sell_fee_usd"], 6.53)
        self.assertEqual(built["fee_drag"]["n"], 106)
        self.assertEqual(built["fee_drag"]["fee_frac"], 0.055467)
        self.assertIn("95 bps/leg", built["fee_drag"]["note"])
        self.assertEqual(built["signal_linkage"]["status"], "known")
        self.assertEqual(built["signal_linkage"]["artifact_count"], 8)
        self.assertEqual(built["signal_linkage"]["outcome_count"], 0)
        self.assertEqual(built["signal_linkage"]["last_generated_at"], "2026-10-01T00:58:40+00:00")
        self.assertEqual(built["oos"]["fee_bps"], 30)
        self.assertIn("95 bps/leg", built["oos"]["note"])
        self.assertIn("190 RT", built["oos"]["note"])
        self.assertIn("T24d will set FEE_BPS", built["oos"]["note"])
        self.assertEqual([row["model"] for row in built["oos"]["models"]], ["rules", "logistic", "lgbm"])
        self.assertEqual(built["oos"]["models"][2]["promoted"], True)
        crypto = next(row for row in summary if row["sleeve"] == "crypto")
        self.assertEqual(built["kill"]["kill_headroom_stored"], crypto["kill_headroom_frac"])
        self.assertEqual(built["kill"]["kill_headroom_frac"], crypto["kill_headroom_frac"] / 100)
        self.assertEqual(built["kill"]["kill_headroom_usd"], 4.04)
        self.assertEqual(built["kill"]["day_kill_pct"], crypto["day_kill_pct"])
        self.assertEqual(built["kill"]["day_target_pct"], crypto["day_target_pct"])
        blob = json.dumps(built)
        self.assertNotIn('"order_id"', blob)
        self.assertNotIn("should-not-survive", blob)
        for row in built["oos"]["models"]:
            self.assertNotIn("sleeve_ir_vs_spy", row)
            self.assertIn("ir_vs_btc", row)

    def test_write_bundle_emits_scorecard_without_order_ids(self):
        with TemporaryDirectory() as tmp:
            target = Path(tmp)
            export_kpi.write_json(
                target / "models.json",
                {
                    "as_of": "2026-09-28T00:16:00Z",
                    "note": "The CLI stays --backend rules.",
                    "models": [{"sleeve": "crypto", "used": "stays --backend rules"}],
                },
            )
            export_kpi.write_json(
                target / "models_oos.json",
                {
                    "updated_at": "2026-09-28T00:16:00Z",
                    "rows": [
                        {
                            "sleeve": "crypto",
                            "model": "lgbm",
                            "promoted": True,
                            "auc": 0.53,
                            "after_cost_mean": 0.0028,
                            "sleeve_ir_vs_spy": 0.1113,
                            "n_long": 3,
                        }
                    ],
                },
            )
            export_kpi.write_bundle(
                target,
                {
                    "kpi_summary": [
                        {
                            "sleeve": "crypto",
                            "as_of": "2026-10-01T00:00:00Z",
                            "kill_headroom_frac": 1.1,
                            "day_kill_pct": -0.1,
                            "day_target_pct": 0.025,
                        }
                    ],
                    "kpi_trades_scrubbed": [
                        {
                            "sleeve": "crypto",
                            "side": "sell",
                            "pnl_frac_of_book": 0.01,
                            "order_id": "should-not-survive",
                        }
                    ],
                    "meta": {"fetched_at": "2026-10-01T00:00:00Z"},
                    "models_oos": None,
                },
            )
            card = json.loads((target / "model_scorecard.json").read_text(encoding="utf-8"))
        self.assertEqual(card["live_backend"]["id"], "rules")
        self.assertEqual(card["closed_fills"]["wins"], 1)
        self.assertEqual(card["closed_fills"]["deduped"], 0)
        self.assertEqual(card["fee_drag"]["status"], "unknown")
        blob = json.dumps(card)
        self.assertNotIn("should-not-survive", blob)
        self.assertNotIn('"order_id"', blob)


if __name__ == "__main__":
    unittest.main()
