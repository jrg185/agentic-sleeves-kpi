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


if __name__ == "__main__":
    unittest.main()
