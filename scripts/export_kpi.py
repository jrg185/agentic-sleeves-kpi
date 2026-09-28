#!/usr/bin/env python3
"""Export scrubbed Supabase KPI views into data/*.json for GitHub Pages.

The browser never sees this script's credentials. Set:

  SUPABASE_URL
  SUPABASE_SERVICE_ROLE_KEY   PostgREST read of the public views

SUPABASE_URL defaults to the agentic-signals project when unset.
ALPHA_VANTAGE_API_KEY and FINNHUB_API_KEY are not read here. Marks are
applied by scripts/refresh_kpi_snapshots.py before this export.
With no service role key, the script leaves the committed sample JSON
in place and exits 0, unless KPI_REFRESH_EXPECTED=1. In that case a missing
credential or a kpi_summary.as_of older than 15 minutes exits non-zero
and does not rewrite data/*.json. It does not rewrite data/models.json.

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
import urllib.parse
import urllib.request
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
FIXTURES = ROOT / "fixtures"

DEFAULT_URL = "https://bsnqwgbshwszbjncglqx.supabase.co"
DEFAULT_BUCKET = "model-weights"
PROJECT_REF = "bsnqwgbshwszbjncglqx"
MAX_BUCKET_JSON = 200_000
VIEWS = ("kpi_summary", "kpi_trades_scrubbed")
OPTIONAL_VIEWS = ("models_oos",)

# Dropped before JSON is written into the public repo. Financial fraction
# columns are kept. Seed columns (start / seed / book_usd) are kept.
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


def frac(dollars: str | Decimal, seed: Decimal) -> float:
    quant = (Decimal(dollars) / seed).quantize(Decimal("0.0000000001"))
    return float(quant)


def book_frac(running_pnl: str | Decimal, seed: Decimal) -> float:
    """Sleeve book / start. Interim book is start + running P&L, never cash residual."""
    if seed == 0:
        raise ValueError("seed is zero")
    return frac(seed + Decimal(running_pnl), seed)


def sheet_frac(dollars: str | Decimal, seed: Decimal) -> float:
    """Book or P&L ÷ start, rounded to 6 decimals. 323.77/300 → 1.079233."""
    if seed == 0:
        raise ValueError("seed is zero")
    quant = (Decimal(dollars) / seed).quantize(Decimal("0.000001"))
    return float(quant)


# Live public.kpi_summary uses warehouse names. The page reads the right-hand names.
# running_bal_vs_start is book/start and wins over a cash residual still called
# running_balance_frac.
SUMMARY_REMAP = (
    ("running_bal_vs_start", "running_balance_frac"),
    ("pnl_pct_of_book", "running_pnl_frac"),
    ("notes", "note"),
)


def _as_float(value):
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def replace_cash_balance(row: dict) -> dict:
    """Turn a cash residual into book/start when the warehouse book column is absent.

    Book ≈ 1 + running P&L. Cash leftovers (crypto −0.023, equities 0.7) sit below 0.95.
    """
    pnl = _as_float(row.get("running_pnl_frac"))
    if pnl is None:
        return row
    book = float((Decimal(str(pnl)) + 1).quantize(Decimal("0.000001")))
    bal = _as_float(row.get("running_balance_frac"))
    if bal is None or bal < 0.95:
        row["running_balance_frac"] = book
    return row


def reshape_summary_row(row: dict) -> dict:
    """Map a live kpi_summary row onto the page contract."""
    if not isinstance(row, dict):
        return row
    had_warehouse_book = row.get("running_bal_vs_start") is not None
    out = dict(row)
    for src, dest in SUMMARY_REMAP:
        if src not in out:
            continue
        if out[src] is not None:
            out[dest] = out[src]
        del out[src]
    for key in ("running_balance_frac", "running_pnl_frac"):
        value = _as_float(out.get(key))
        if value is not None:
            out[key] = float(Decimal(str(value)).quantize(Decimal("0.000001")))
    if not had_warehouse_book:
        out = replace_cash_balance(out)
    return out


def reshape_trade_row(row: dict) -> dict:
    """Fill running_balance_frac is book at that fill, not cash left over."""
    if not isinstance(row, dict):
        return row
    out = dict(row)
    pnl = _as_float(out.get("running_pnl_frac"))
    if pnl is None:
        return out
    book = float((Decimal(str(pnl)) + 1).quantize(Decimal("0.000001")))
    bal = _as_float(out.get("running_balance_frac"))
    if bal is None or bal < 0.95:
        out["running_balance_frac"] = book
    return out


def sample_bundle() -> dict:
    """Stand-in rows in the scrubbed view shape. Not a live fetch.

    running_balance_frac is sleeve book / start. Until true mark-to-market,
    book = start + running P&L. Cash left after a fill is not the balance.
    """
    crypto = SEEDS["crypto"]
    equities = SEEDS["equities"]
    combined = crypto + equities
    # Crypto Desk rebuild: book 323.77 = start 300 + 23.77 (realized +6.24 plus uPnL).
    # Equities book stays 500.87. Combined 323.77 + 500.87 = 824.64.
    crypto_pnl = "23.77"
    equities_pnl = "0.87"
    combined_pnl = "24.64"
    as_of = "2026-09-27T22:06:00-04:00"
    summary = [
        {
            "sleeve": "crypto",
            "as_of": as_of,
            "running_balance_frac": sheet_frac("323.77", crypto),
            "running_pnl_frac": sheet_frac(crypto_pnl, crypto),
            "day_pnl_frac": None,
            "day_kill_pct": -0.10,
            "kill_headroom_frac": frac("30", crypto),
            "day_target_pct": 0.025,
            "note": "Realized +$6.24. Book is start + realized + uPnL. Day target is realized-only.",
        },
        {
            "sleeve": "equities",
            "as_of": as_of,
            "running_balance_frac": sheet_frac("500.87", equities),
            "running_pnl_frac": sheet_frac(equities_pnl, equities),
            "day_pnl_frac": None,
            "day_kill_pct": -0.25,
            "kill_headroom_frac": frac("125", equities),
            "day_target_pct": None,
            "note": "Rails are percent of book.",
        },
        {
            "sleeve": "combined",
            "as_of": as_of,
            "running_balance_frac": sheet_frac("824.64", combined),
            "running_pnl_frac": sheet_frac(combined_pnl, combined),
            "day_pnl_frac": 0,
            "day_kill_pct": None,
            "kill_headroom_frac": frac("155", combined),
            "day_target_pct": None,
            "note": "Per-sleeve kill rails. Crypto day target is realized-only.",
        },
    ]
    # pnl and running P&L are dollars. Book at the fill is start + running P&L.
    trade_src = [
        ("crypto", "2026-09-27T09:58:00-04:00", "QNT", "sell", 0.1031, "3.98", "3.98", "+15% scale"),
        ("crypto", "2026-09-27T12:50:00-04:00", "W", "sell", 1058, "2.20", "6.18", "+15% scale"),
        ("crypto", "2026-09-27T14:55:00-04:00", "GRT", "buy", 886.2, "0", "6.18", "artifact buy"),
        ("crypto", "2026-09-27T17:56:00-04:00", "GRT", "sell", 443.1, "1.85", "8.03", "+15% scale"),
        ("crypto", "2026-09-27T17:56:00-04:00", "ORCA", "sell", 15.13, "0.50", "8.53", "rotation"),
        ("crypto", "2026-09-27T17:56:00-04:00", "NEAR", "buy", 4.93, "0", "8.53", "rotation"),
        ("crypto", "2026-09-27T17:56:00-04:00", "IMX", "buy", 149.5, "0", "8.53", "rotation"),
        ("equities", "2026-09-25T15:37:00-04:00", "QCOM", "buy", 0.743509, "0", "0", "swing entry"),
    ]
    trades = []
    for sleeve, ts, ticker, side, qty, pnl, run_pnl, why in trade_src:
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
                "running_balance_frac": sheet_frac(seed + Decimal(run_pnl), seed),
                "why": why,
            }
        )
    meta = {
        "source": "sample",
        "fetched_at": None,
        "project_ref": PROJECT_REF,
        "views": [f"public.{name}" for name in VIEWS],
        "note": (
            "Sample snapshot. Balances are sleeve book from the sheet desks (crypto $323.77, realized +$6.24; equities $500.87; combined $824.64), not cash. "
            "Replaced when the export Action can read the Supabase views. "
            "Dollar figures on the page are seed × fraction."
        ),
    }
    models_oos = {
        "rows": [],
        "note": "No out-of-sample model rows in this snapshot.",
    }
    models = {
        "as_of": as_of,
        "status": "placeholder",
        "note": "OOS metrics stay empty until T04 publishes them. Replace data/models.json; the page reads these fields.",
        "models": [
            {
                "sleeve": "crypto",
                "name": "Crypto v0 heuristic",
                "used": "Shadow advisory for the $300 crypto sleeve. Scores 24h return, volume z, and an ATR-ish range. It does not place orders.",
                "training": "No fit. Buy when 24h return is above +2% and volume z is above 0.5. Sell when 24h return is below -2%.",
                "data_source": "Coinbase Exchange public hourly candles, 168 bars. 57 Robinhood USD names that also have a Coinbase product.",
                "oos": {
                    "status": "placeholder",
                    "window": None,
                    "hit_rate": None,
                    "avg_return": None,
                    "n": None,
                    "note": "Pending T04.",
                },
            },
            {
                "sleeve": "equities",
                "name": "Equities sleeve",
                "used": "No fitted equities model is published on this board.",
                "training": "Not published.",
                "data_source": "Scrubbed equities fills only. This page has no broker feed.",
                "oos": {
                    "status": "placeholder",
                    "window": None,
                    "hit_rate": None,
                    "avg_return": None,
                    "n": None,
                    "note": "Pending T04.",
                },
            },
        ],
    }
    return {
        "kpi_summary": summary,
        "kpi_trades_scrubbed": trades,
        "models_oos": models_oos,
        "models": models,
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
    write_json(target / "models.json", bundle["models"])
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


def storage_call(base_url: str, key: str, path: str, body: dict | None = None) -> bytes:
    request = urllib.request.Request(
        base_url.rstrip("/") + path,
        data=None if body is None else json.dumps(body).encode("utf-8"),
        method="POST" if body is not None else "GET",
        headers={
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "agentic-sleeves-kpi-export",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:120]
        if key and key in detail:
            detail = detail.replace(key, "[redacted]")
        raise RuntimeError(f"storage HTTP {exc.code}") from None


def rows_from_oos_payload(payload):
    if isinstance(payload, list):
        return [scrub_row(row) for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict) and isinstance(payload.get("rows"), list):
        return [scrub_row(row) for row in payload["rows"] if isinstance(row, dict)]
    return None


def fetch_bucket_oos(base_url: str, key: str, bucket: str) -> list | None:
    """Pull a small models/OOS JSON from storage when the view is absent."""
    try:
        listed = json.loads(
            storage_call(
                base_url,
                key,
                f"/storage/v1/object/list/{urllib.parse.quote(bucket)}",
                {"prefix": "", "limit": 100},
            ).decode("utf-8")
        )
    except Exception as exc:
        print(f"Storage list skipped: {exc}", file=sys.stderr)
        return None
    if not isinstance(listed, list):
        return None
    names = []
    for item in listed:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "")
        lowered = name.lower()
        if lowered.endswith(".json") and ("oos" in lowered or "model" in lowered):
            names.append(name)
    for name in names:
        encoded = urllib.parse.quote(name)
        try:
            raw = storage_call(base_url, key, f"/storage/v1/object/{urllib.parse.quote(bucket)}/{encoded}")
        except Exception as exc:
            print(f"Storage object skipped: {exc}", file=sys.stderr)
            continue
        if len(raw) > MAX_BUCKET_JSON:
            continue
        try:
            payload = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            continue
        rows = rows_from_oos_payload(payload)
        if rows is not None:
            return rows
    return None


def export_live(base_url: str, key: str | None, db_url: str | None) -> dict:
    rows = {}
    for view in VIEWS:
        rows[view] = load_view(base_url, key, db_url, view)
    rows["kpi_summary"] = [reshape_summary_row(row) for row in rows["kpi_summary"]]
    rows["kpi_trades_scrubbed"] = [reshape_trade_row(row) for row in rows["kpi_trades_scrubbed"]]
    missing = []
    for view in OPTIONAL_VIEWS:
        try:
            rows[view] = load_view(base_url, key, db_url, view)
        except ViewMissing:
            missing.append(view)
            rows[view] = None
    # A missing or empty models_oos view must not wipe a committed seed.
    # The page keeps data/models_oos.json until the view returns rows.
    present = [name for name in (*VIEWS, *OPTIONAL_VIEWS) if name not in missing]
    rows["meta"] = {
        "source": "supabase",
        "fetched_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "project_ref": PROJECT_REF,
        "views": [f"public.{name}" for name in present],
        "row_counts": {
            name: len(rows[name]) for name in present if isinstance(rows[name], list)
        },
        "note": (
            "Exported from scrubbed views. Balances are sleeve book (cash + MTM; interim start + running P&L), not cash. "
            "kpi_summary remaps running_bal_vs_start, pnl_pct_of_book, and notes "
            "onto running_balance_frac, running_pnl_frac, and note. "
            "Page dollars are seed × fraction."
        ),
    }
    if missing:
        rows["meta"]["optional_missing"] = missing
    return rows


def oos_has_rows(payload) -> bool:
    if isinstance(payload, list):
        return any(isinstance(row, dict) for row in payload)
    if isinstance(payload, dict):
        rows = payload.get("rows")
        return isinstance(rows, list) and any(isinstance(row, dict) for row in rows)
    return False


def parse_as_of(value) -> dt.datetime:
    text = str(value).strip().replace("Z", "+00:00")
    if " " in text and "T" not in text:
        text = text.replace(" ", "T", 1)
    parsed = dt.datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def assert_summary_fresh(rows: list, *, now: dt.datetime | None = None) -> None:
    """Refuse to write JSON when the view is still the pre-refresh snapshot."""
    if not rows:
        raise RuntimeError("kpi_summary is empty after refresh; not writing JSON")
    current = now or dt.datetime.now(dt.timezone.utc)
    for row in rows:
        as_of = row.get("as_of") if isinstance(row, dict) else None
        if not as_of:
            raise RuntimeError("kpi_summary row is missing as_of after refresh; not writing JSON")
        moment = parse_as_of(as_of)
        if current - moment > dt.timedelta(minutes=15) or moment - current > dt.timedelta(minutes=5):
            raise RuntimeError(
                f"kpi_summary as_of {moment.strftime('%Y-%m-%dT%H:%M:%SZ')} is stale after refresh; "
                "not writing JSON"
            )


def write_bundle(target: Path, bundle: dict) -> None:
    for name in (*VIEWS, "meta"):
        write_json(target / f"{name}.json", bundle[name])
    oos = bundle.get("models_oos")
    if oos_has_rows(oos):
        if isinstance(oos, list):
            oos = {"rows": oos, "updated_at": bundle["meta"]["fetched_at"]}
        write_json(target / "models_oos.json", oos)


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

    refresh_expected = (os.environ.get("KPI_REFRESH_EXPECTED") or "").strip().lower() in {
        "1",
        "true",
        "yes",
    }
    if not key and not db_url:
        if refresh_expected:
            print(
                "KPI_REFRESH_EXPECTED is set but Supabase credentials are missing; "
                "refusing to leave KPI JSON unchanged.",
                file=sys.stderr,
            )
            return 1
        print(
            "SUPABASE_SERVICE_ROLE_KEY and SUPABASE_DB_URL are unset; "
            "leaving committed data/*.json in place."
        )
        needed = [DATA / f"{name}.json" for name in (*VIEWS, *OPTIONAL_VIEWS, "models", "meta")]
        if any(not path.exists() for path in needed):
            print("Sample JSON missing; installing fixtures.", file=sys.stderr)
            install_sample(DATA)
        return 0

    bundle = export_live(base_url, key, db_url)
    if refresh_expected:
        assert_summary_fresh(bundle["kpi_summary"])
    write_bundle(DATA, bundle)
    if not (DATA / "models.json").exists():
        write_json(DATA / "models.json", sample_bundle()["models"])
    print(
        "Exported "
        + ", ".join(f"{name}={bundle['meta']['row_counts'][name]}" for name in VIEWS)
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
