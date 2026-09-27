#!/usr/bin/env python3
"""Export scrubbed Supabase KPI views into data/*.json for GitHub Pages.

The browser never sees this script's credentials. Set one of:

  SUPABASE_SERVICE_ROLE_KEY   preferred; PostgREST read of the public views
  SUPABASE_DB_URL             optional read-only Postgres URI

SUPABASE_URL defaults to the agentic-signals project. With neither secret,
the script leaves the committed sample JSON in place and exits 0.

Views (fraction / percent rails; no PII):
  public.kpi_summary
  public.kpi_trades_scrubbed

Optional, written when the view exists and skipped when it does not:
  public.models_oos
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import urllib.error
import urllib.request
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
FIXTURES = ROOT / "fixtures"

DEFAULT_URL = "https://bsnqwgbshwszbjncglqx.supabase.co"
PROJECT_REF = "bsnqwgbshwszbjncglqx"
VIEWS = ("kpi_summary", "kpi_trades_scrubbed")
OPTIONAL_VIEWS = ("models_oos",)

DENY_KEYS = {
    "email",
    "phone",
    "order_id",
    "account_id",
    "user_id",
    "api_key",
    "service_role",
    "secret",
    "password",
    "token",
    "ssn",
    "address",
}

SEEDS = {"crypto": Decimal("300"), "equities": Decimal("500")}


def frac(dollars: str, seed: Decimal) -> float:
    quant = (Decimal(dollars) / seed).quantize(Decimal("0.0000000001"))
    return float(quant)


def sample_bundle() -> dict:
    """Stand-in rows in the scrubbed view shape. Not a live fetch."""
    crypto = SEEDS["crypto"]
    equities = SEEDS["equities"]
    combined = crypto + equities
    as_of = "2026-09-27T22:06:00-04:00"
    summary = [
        {
            "sleeve": "crypto",
            "as_of": as_of,
            "running_balance_frac": frac("-6.91", crypto),
            "running_pnl_frac": frac("8.53", crypto),
            "day_pnl_frac": None,
            "day_kill_pct": -0.10,
            "kill_headroom_frac": frac("30", crypto),
            "day_target_pct": 0.025,
            "note": "Day target is realized-only. Rails are percent of book.",
        },
        {
            "sleeve": "equities",
            "as_of": as_of,
            "running_balance_frac": frac("350", equities),
            "running_pnl_frac": frac("0", equities),
            "day_pnl_frac": None,
            "day_kill_pct": -0.25,
            "kill_headroom_frac": frac("125", equities),
            "day_target_pct": None,
            "note": "Rails are percent of book.",
        },
        {
            "sleeve": "combined",
            "as_of": as_of,
            "running_balance_frac": frac("343.09", combined),
            "running_pnl_frac": frac("8.53", combined),
            "day_pnl_frac": 0,
            "day_kill_pct": None,
            "kill_headroom_frac": frac("155", combined),
            "day_target_pct": None,
            "note": "Per-sleeve kill rails. Crypto day target is realized-only.",
        },
    ]
    trade_src = [
        ("crypto", "2026-09-27T09:58:00-04:00", "QNT", "sell", 0.1031, "3.98", "3.98", "16.42", "+15% scale"),
        ("crypto", "2026-09-27T12:50:00-04:00", "W", "sell", 1058, "2.20", "6.18", "32.69", "+15% scale"),
        ("crypto", "2026-09-27T14:55:00-04:00", "GRT", "buy", 886.2, "0", "6.18", "5.51", "artifact buy"),
        ("crypto", "2026-09-27T17:56:00-04:00", "GRT", "sell", 443.1, "1.85", "8.03", "20.82", "+15% scale"),
        ("crypto", "2026-09-27T17:56:00-04:00", "ORCA", "sell", 15.13, "0.50", "8.53", "47.38", "rotation"),
        ("crypto", "2026-09-27T17:56:00-04:00", "NEAR", "buy", 4.93, "0", "8.53", "20.03", "rotation"),
        ("crypto", "2026-09-27T17:56:00-04:00", "IMX", "buy", 149.5, "0", "8.53", "-6.91", "rotation"),
        ("equities", "2026-09-25T15:37:00-04:00", "QCOM", "buy", 0.743509, "0", "0", "350", "swing entry"),
    ]
    trades = []
    for sleeve, ts, ticker, side, qty, pnl, run_pnl, bal, why in trade_src:
        seed = SEEDS[sleeve]
        trades.append(
            {
                "sleeve": sleeve,
                "ts": ts,
                "ticker": ticker,
                "side": side,
                "qty": qty,
                "pnl_frac": frac(pnl, seed),
                "running_pnl_frac": frac(run_pnl, seed),
                "running_balance_frac": frac(bal, seed),
                "why": why,
            }
        )
    meta = {
        "source": "sample",
        "fetched_at": None,
        "project_ref": PROJECT_REF,
        "views": [f"public.{name}" for name in VIEWS],
        "note": (
            "Sample snapshot in the scrubbed view shape. "
            "Replaced when the export Action can read the Supabase views. "
            "Dollar figures on the page are seed × fraction."
        ),
    }
    models_oos = {
        "rows": [],
        "note": "No out-of-sample model rows in this snapshot.",
    }
    return {
        "kpi_summary": summary,
        "kpi_trades_scrubbed": trades,
        "models_oos": models_oos,
        "meta": meta,
    }


class ViewMissing(RuntimeError):
    """Raised when an optional view is not in the database."""


def looks_like_secret(value: object) -> bool:
    if not isinstance(value, str):
        return False
    text = value.strip()
    if text.startswith("eyJ") and text.count(".") >= 2:
        return True
    lowered = text.lower()
    return "service_role" in lowered or lowered.startswith("sb_secret_")


def scrub_row(row: dict) -> dict:
    clean = {}
    for key, value in row.items():
        if str(key).lower() in DENY_KEYS:
            continue
        if looks_like_secret(value):
            continue
        clean[str(key)] = jsonable(value)
    return clean


def jsonable(value):
    if isinstance(value, (dt.datetime, dt.date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dict):
        return scrub_row(value)
    if isinstance(value, (list, tuple)):
        return [jsonable(item) for item in value]
    return value


def write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def install_sample(target: Path) -> None:
    bundle = sample_bundle()
    write_json(target / "kpi_summary.json", bundle["kpi_summary"])
    write_json(target / "kpi_trades_scrubbed.json", bundle["kpi_trades_scrubbed"])
    write_json(target / "models_oos.json", bundle["models_oos"])
    write_json(target / "meta.json", bundle["meta"])


def fetch_rest(base_url: str, key: str, view: str) -> list:
    url = base_url.rstrip("/") + f"/rest/v1/{view}?select=*"
    request = urllib.request.Request(
        url,
        headers={
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Accept": "application/json",
            "User-Agent": "agentic-sleeves-kpi-export",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:180]
        if key and key in detail:
            detail = detail.replace(key, "[redacted]")
        if exc.code == 404 or "PGRST205" in detail:
            raise ViewMissing(view) from None
        raise RuntimeError(f"REST {view} HTTP {exc.code}: {detail}") from None
    payload = json.loads(body)
    if not isinstance(payload, list):
        raise RuntimeError(f"REST {view} did not return a row list")
    return [scrub_row(row) for row in payload]


def fetch_db(db_url: str, view: str) -> list:
    try:
        import psycopg
        from psycopg.errors import UndefinedTable
    except ImportError as exc:
        raise RuntimeError("psycopg is required for SUPABASE_DB_URL") from exc
    sql = f"select * from public.{view}"
    try:
        with psycopg.connect(db_url, connect_timeout=20) as conn:
            with conn.cursor() as cur:
                cur.execute(sql)
                columns = [desc.name for desc in cur.description]
                return [scrub_row(dict(zip(columns, row))) for row in cur.fetchall()]
    except UndefinedTable:
        raise ViewMissing(view) from None


def load_view(base_url: str, key: str | None, db_url: str | None, view: str) -> list:
    if key:
        try:
            return fetch_rest(base_url, key, view)
        except ViewMissing:
            if not db_url:
                raise
        except Exception:
            if not db_url:
                raise
            print(f"REST read of {view} failed; trying SUPABASE_DB_URL", file=sys.stderr)
    if not db_url:
        raise RuntimeError("No Supabase credential")
    return fetch_db(db_url, view)


def export_live(base_url: str, key: str | None, db_url: str | None) -> dict:
    rows = {}
    for view in VIEWS:
        rows[view] = load_view(base_url, key, db_url, view)
    missing = []
    for view in OPTIONAL_VIEWS:
        try:
            rows[view] = load_view(base_url, key, db_url, view)
        except ViewMissing:
            missing.append(view)
            rows[view] = {
                "rows": [],
                "note": f"{view} view was not present at export time.",
            }
    present = [name for name in (*VIEWS, *OPTIONAL_VIEWS) if name not in missing]
    rows["meta"] = {
        "source": "supabase",
        "fetched_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "project_ref": PROJECT_REF,
        "views": [f"public.{name}" for name in present],
        "row_counts": {
            name: len(rows[name]) for name in present if isinstance(rows[name], list)
        },
        "note": "Exported from scrubbed views. Page dollars are seed × fraction.",
    }
    if missing:
        rows["meta"]["optional_missing"] = missing
    return rows


def write_bundle(target: Path, bundle: dict) -> None:
    for name in (*VIEWS, *OPTIONAL_VIEWS, "meta"):
        write_json(target / f"{name}.json", bundle[name])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--install-sample",
        action="store_true",
        help="Write the committed sample into data/ and fixtures/ and exit",
    )
    args = parser.parse_args(argv)

    if args.install_sample:
        install_sample(DATA)
        install_sample(FIXTURES)
        print("Installed sample JSON into data/ and fixtures/")
        return 0

    key = (os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or "").strip() or None
    db_url = (os.environ.get("SUPABASE_DB_URL") or "").strip() or None
    base_url = (os.environ.get("SUPABASE_URL") or DEFAULT_URL).strip()

    if not key and not db_url:
        print(
            "SUPABASE_SERVICE_ROLE_KEY and SUPABASE_DB_URL are unset; "
            "leaving committed data/*.json in place."
        )
        needed = [DATA / f"{name}.json" for name in (*VIEWS, *OPTIONAL_VIEWS, "meta")]
        if any(not path.exists() for path in needed):
            print("Sample JSON missing; installing fixtures.", file=sys.stderr)
            install_sample(DATA)
        return 0

    bundle = export_live(base_url, key, db_url)
    write_bundle(DATA, bundle)
    print(
        "Exported "
        + ", ".join(f"{name}={bundle['meta']['row_counts'][name]}" for name in VIEWS)
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
