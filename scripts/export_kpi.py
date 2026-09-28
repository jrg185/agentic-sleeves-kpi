#!/usr/bin/env python3
"""Export scrubbed Supabase KPI views into data/*.json for GitHub Pages.

The browser never sees this script's credentials. Set:

  SUPABASE_URL
  SUPABASE_SERVICE_ROLE_KEY   PostgREST read of the public views

SUPABASE_URL defaults to the agentic-signals project when unset.
ALPHA_VANTAGE_API_KEY and FINNHUB_API_KEY are not read here. Marks are
applied by scripts/refresh_kpi_snapshots.py before this export.
With no service role key, the script leaves the committed KPI JSON
in place, stamps data/meta.json with export_status "stale", and exits 0,
unless KPI_REFRESH_EXPECTED=1. In that case a missing credential or the
latest kpi_summary.as_of per sleeve older than 15 minutes exits non-zero,
stamps meta.json, and does not rewrite KPI numbers. Older snapshots for
the same sleeve are ignored. It does not rewrite data/models.json.

  python3 scripts/export_kpi.py --self-test

Views (fraction / percent rails; no PII):
  public.kpi_summary
  public.kpi_trades_scrubbed

Derived, scrubbed before they are written (no raw dollar columns, no account ids):
  public.kpi_trades → data/open_positions.json
    Net open qty by ticker and sleeve, marked with the same public quotes as
    scripts/refresh_kpi_snapshots.py. unrealized_pnl_frac is that P&L ÷ sleeve seed.
  public.kpi_sleeve_snapshots → data/sleeve_curves.json
    History as fractions of the book seed (crypto 300, equities 500, combined 800).

Optional, written when the view exists and skipped when it does not:
  public.models_oos
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal, ROUND_HALF_UP
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
# Same book seeds as derive.js SEEDS_USD. Curves and open P&L use these divisors,
# not a raw account balance, so the page can show seed × fraction.
BOOK_SEEDS = {
    "crypto": Decimal("300"),
    "equities": Decimal("500"),
    "combined": Decimal("800"),
}
SNAPSHOT_USD = (
    ("realized_pnl_usd", "realized_pnl_frac"),
    ("unrealized_pnl_usd", "unrealized_pnl_frac"),
    ("running_pnl_usd", "running_pnl_frac"),
    ("running_balance_usd", "running_balance_frac"),
    ("day_pnl_usd", "day_pnl_frac"),
)


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
    """Keep the row. Running ledger is applied later by attach_running_ledger."""
    if not isinstance(row, dict):
        return row
    return dict(row)


# Tape ledger seeds. Same dollars as derive.js SEEDS_USD. Not read from a sheet.
LEDGER_SEEDS = {"crypto": Decimal("300"), "equities": Decimal("500")}
LEDGER_QUANT = Decimal("0.000001")
MACHINE_WHY = re.compile(
    r"^RH Agentic (?:backfill|sync) order "
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


def q6(value: Decimal) -> Decimal:
    """Six-decimal fraction. Half away from zero, matching Postgres round(numeric, 6)."""
    return value.quantize(LEDGER_QUANT, rounding=ROUND_HALF_UP)


def _decimal_or_none(value):
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def _text_or_none(value):
    if value is None:
        return None
    return str(value)


def full_why(row: dict):
    """Return the full note. Never slices.

    A machine `RH Agentic backfill|sync order <uuid>` yields to a human notes
    field when that field is present. A why that is only a prefix of notes
    (view left()) yields to the longer notes text.
    """
    if not isinstance(row, dict):
        return None
    why_text = _text_or_none(row.get("why"))
    notes_value = row.get("notes") if "notes" in row else row.get("note")
    notes_text = _text_or_none(notes_value)
    why_stripped = "" if why_text is None else why_text.strip()
    notes_stripped = "" if notes_text is None else notes_text.strip()
    notes_human = bool(notes_stripped) and not MACHINE_WHY.match(notes_stripped)
    if notes_human and (
        MACHINE_WHY.match(why_stripped)
        or (why_stripped and notes_text.startswith(why_text) and len(notes_text) > len(why_text))
    ):
        return notes_text
    if why_text is not None:
        return why_text
    if notes_human:
        return notes_text
    return None


def _trade_pnl_usd(row: dict, seed: Decimal, use_dollars: bool) -> Decimal:
    if use_dollars:
        pnl = _decimal_or_none(row.get("pnl_trade_usd"))
        if pnl is None:
            pnl = _decimal_or_none(row.get("pnl_usd"))
        if pnl is not None:
            return pnl
    frac = _decimal_or_none(row.get("pnl_frac_of_book"))
    if frac is None:
        frac = _decimal_or_none(row.get("pnl_frac"))
    if frac is None:
        return Decimal("0")
    return frac * seed


def attach_running_ledger(rows: list) -> list:
    """Regenerate running realized P&L and book balance per sleeve.

    Chronological (timestamp, ticker, side, original index). Book at the fill
    is seed + cumulative pnl_trade_usd, as a fraction of the sleeve seed.
    Opening fills with a null trade P&L count as zero. Does not read a sheet.
    Overwrites any running_* the view already sent. Does not truncate why.
    """
    out = [dict(row) if isinstance(row, dict) else row for row in rows]
    grouped: dict[str, list[int]] = {}
    for index, row in enumerate(out):
        if not isinstance(row, dict):
            continue
        sleeve = str(row.get("sleeve") or "").strip().lower()
        grouped.setdefault(sleeve, []).append(index)
    for sleeve, indexes in grouped.items():
        seed = LEDGER_SEEDS.get(sleeve)
        if seed is None or seed == 0:
            continue
        use_dollars = any(
            _decimal_or_none(out[i].get("pnl_trade_usd")) is not None
            or _decimal_or_none(out[i].get("pnl_usd")) is not None
            for i in indexes
        )
        ordered = sorted(
            indexes,
            key=lambda i: (
                str(out[i].get("timestamp_et") or out[i].get("ts") or ""),
                str(out[i].get("ticker") or ""),
                str(out[i].get("side") or ""),
                i,
            ),
        )
        cum = Decimal("0")
        for i in ordered:
            row = out[i]
            cum += _trade_pnl_usd(row, seed, use_dollars)
            running = q6(cum / seed)
            balance = q6((seed + cum) / seed)
            row["running_pnl_frac"] = float(running)
            row["running_balance_frac"] = float(balance)
            why = full_why(row)
            if why is not None:
                row["why"] = why
    return out


def normalize_sleeve(value) -> str | None:
    key = str(value or "").strip().lower()
    if key == "equity":
        return "equities"
    if key in BOOK_SEEDS:
        return key
    return None


def _ratio(dollars: Decimal, seed: Decimal) -> float:
    return float(q6(dollars / seed))


def scrub_snapshot_history(rows: list) -> list:
    """Warehouse sleeve snapshots as fractions of the book seed.

    Copies only sleeve, as_of, and fraction fields. Dollar columns, notes, and
    account ids are not written. A row with no book fraction is skipped so the
    chart does not invent a point.
    """
    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        sleeve = normalize_sleeve(row.get("sleeve"))
        as_of = row.get("as_of")
        seed = BOOK_SEEDS.get(sleeve) if sleeve else None
        if sleeve is None or seed is None or not as_of:
            continue
        item = {"sleeve": sleeve, "as_of": jsonable(as_of)}
        for src, dest in SNAPSHOT_USD:
            dollars = _decimal_or_none(row.get(src))
            if dollars is not None:
                item[dest] = _ratio(dollars, seed)
                continue
            existing = _decimal_or_none(row.get(dest))
            if existing is not None:
                item[dest] = float(q6(existing))
        if "running_balance_frac" not in item and "running_pnl_frac" in item:
            item["running_balance_frac"] = float(q6(Decimal("1") + Decimal(str(item["running_pnl_frac"]))))
        if "running_balance_frac" not in item:
            continue
        out.append(item)
    out.sort(key=lambda item: (str(item["as_of"]), item["sleeve"]))
    return out


def scrub_open_positions(fills: list, marks: dict, as_of: str) -> dict:
    """Net open qty by ticker and sleeve. Unrealized is P&L ÷ sleeve seed.

    `marks` are prices from the same quote path as refresh_kpi_snapshots.
    A missing mark is an error. The JSON has avg and mark prices plus qty,
    and does not carry warehouse dollar columns or account ids.
    """
    import refresh_kpi_snapshots as refresh

    try:
        _realized, book = refresh.apply_books(fills)
    except refresh.RefreshError as exc:
        raise RuntimeError(str(exc)) from None
    missing = [f"{sleeve} {ticker}" for sleeve, ticker in sorted(book) if (sleeve, ticker) not in marks]
    if missing:
        raise RuntimeError("open tickers have no mark: " + ", ".join(missing))
    order = {"crypto": 0, "equities": 1}
    positions = []
    for (sleeve, ticker), pos in sorted(book.items(), key=lambda item: (order.get(item[0][0], 9), item[0][1])):
        qty = pos["qty"]
        if abs(qty) <= refresh.DUST:
            continue
        seed = BOOK_SEEDS.get(sleeve)
        if seed is None:
            raise RuntimeError(f"open sleeve {sleeve} has no book seed")
        avg = pos["avg"]
        mark = Decimal(str(marks[(sleeve, ticker)]))
        unreal = qty * (mark - avg)
        positions.append(
            scrub_row(
                {
                    "sleeve": sleeve,
                    "ticker": ticker,
                    "side": "long" if qty > 0 else "short",
                    "qty": format(abs(qty), "f"),
                    "avg": format(avg, "f"),
                    "mark": format(mark, "f"),
                    "unrealized_pnl_frac": _ratio(unreal, seed),
                }
            )
        )
    return {"as_of": as_of, "positions": positions}


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
        "open_positions": {
            "as_of": as_of,
            "positions": [],
            "note": "Sample snapshot has no marked open book.",
        },
        "sleeve_curves": {
            "updated_at": as_of,
            "series": [],
            "note": "Sample snapshot has no sleeve history.",
        },
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
    write_json(target / "open_positions.json", bundle["open_positions"])
    write_json(target / "sleeve_curves.json", bundle["sleeve_curves"])
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


def fetch_rest_paged(base_url: str, key: str, view: str, order: str) -> list:
    """Read a public table in pages. Refuses a truncated curve or book."""
    rows: list = []
    page = 1000
    max_pages = 40
    for page_index in range(max_pages):
        query = urllib.parse.urlencode(
            {
                "select": "*",
                "order": order,
                "limit": str(page),
                "offset": str(page_index * page),
            }
        )
        url = base_url.rstrip("/") + f"/rest/v1/{view}?{query}"
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
            with urllib.request.urlopen(request, timeout=40) as response:
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
        rows.extend(scrub_row(row) for row in payload if isinstance(row, dict))
        if len(payload) < page:
            return rows
    raise RuntimeError(f"{view} exceeded {page * max_pages} rows; refusing a partial export")


def load_trades_for_positions(base_url: str, key: str | None, db_url: str | None) -> list:
    import refresh_kpi_snapshots as refresh

    secrets = [key or "", db_url or ""]
    if key:
        try:
            return refresh.fetch_trades_rest(base_url, key)
        except refresh.RefreshError as exc:
            if not db_url:
                raise RuntimeError(refresh.redact(str(exc), secrets)) from None
            print(f"REST read of kpi_trades failed; trying SUPABASE_DB_URL", file=sys.stderr)
    if not db_url:
        raise RuntimeError("No Supabase credential")
    return refresh.fetch_trades_db(db_url, secrets)


def load_open_positions(base_url: str, key: str | None, db_url: str | None, as_of: str) -> dict:
    """Replay kpi_trades and mark opens. Does not invent a price."""
    import refresh_kpi_snapshots as refresh

    fills = load_trades_for_positions(base_url, key, db_url)
    try:
        _realized, book = refresh.apply_books(fills)
    except refresh.RefreshError as exc:
        raise RuntimeError(refresh.redact(str(exc), [key or "", db_url or ""])) from None
    if not book:
        return {"as_of": as_of, "positions": []}
    env = refresh.env_values()
    if base_url:
        env["SUPABASE_URL"] = base_url
    if key:
        env["SUPABASE_SERVICE_ROLE_KEY"] = key
    if db_url:
        env["SUPABASE_DB_URL"] = db_url
    secrets = [
        env.get("SUPABASE_SERVICE_ROLE_KEY", ""),
        env.get("SUPABASE_DB_URL", ""),
        env.get("FINNHUB_API_KEY", ""),
        env.get("COINSTATS_API_KEY", ""),
        env.get("ALPHA_VANTAGE_API_KEY", ""),
    ]
    try:
        quotes = refresh.resolve_marks(sorted(book), env)
    except refresh.RefreshError as exc:
        raise RuntimeError(refresh.redact(str(exc), secrets)) from None
    marks = {pair: price for pair, (price, _source) in quotes.items()}
    return scrub_open_positions(fills, marks, as_of)


def load_sleeve_curves(base_url: str, key: str | None, db_url: str | None, updated_at: str) -> dict:
    if key:
        try:
            raw = fetch_rest_paged(base_url, key, "kpi_sleeve_snapshots", "as_of.asc")
        except ViewMissing:
            if not db_url:
                raise
            raw = fetch_db(db_url, "kpi_sleeve_snapshots")
        except Exception:
            if not db_url:
                raise
            print("REST read of kpi_sleeve_snapshots failed; trying SUPABASE_DB_URL", file=sys.stderr)
            raw = fetch_db(db_url, "kpi_sleeve_snapshots")
    else:
        if not db_url:
            raise RuntimeError("No Supabase credential")
        raw = fetch_db(db_url, "kpi_sleeve_snapshots")
    return {"updated_at": updated_at, "series": scrub_snapshot_history(raw)}


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
    rows["kpi_summary"] = latest_summary_rows(rows["kpi_summary"])
    rows["kpi_trades_scrubbed"] = attach_running_ledger(
        [reshape_trade_row(row) for row in rows["kpi_trades_scrubbed"]]
    )
    fetched_at = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows["open_positions"] = load_open_positions(base_url, key, db_url, fetched_at)
    rows["sleeve_curves"] = load_sleeve_curves(base_url, key, db_url, fetched_at)
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
    present.extend(["kpi_trades", "kpi_sleeve_snapshots"])
    rows["meta"] = {
        "source": "supabase",
        "fetched_at": fetched_at,
        "export_status": "ok",
        "export_error": None,
        "export_attempted_at": fetched_at,
        "project_ref": PROJECT_REF,
        "views": [f"public.{name}" for name in present],
        "row_counts": {
            name: len(rows[name]) for name in present if isinstance(rows.get(name), list)
        },
        "note": (
            "Exported from scrubbed views. Balances are sleeve book (cash + MTM; interim start + running P&L), not cash. "
            "kpi_summary remaps running_bal_vs_start, pnl_pct_of_book, and notes "
            "onto running_balance_frac, running_pnl_frac, and note. "
            "Tape running_pnl_frac and running_balance_frac are regenerated per sleeve "
            "from warehouse trade P&L (seed + cumulative realized), not from a sheet. "
            "why is the full note. "
            "Page dollars are seed × fraction. "
            "Sleeve as_of comes from kpi_sleeve_snapshots, inserted by "
            "scripts/refresh_kpi_snapshots.py before this export. "
            "open_positions.json is net open qty from kpi_trades, marked with the same "
            "public quotes as that refresh; unrealized_pnl_frac is the open P&L divided "
            "by the sleeve seed. sleeve_curves.json is snapshot history as fractions of "
            "the book seed. Raw dollar columns and account ids are not written."
        ),
        "warehouse_status": "ok",
    }
    rows["meta"]["row_counts"]["open_positions"] = len(rows["open_positions"]["positions"])
    rows["meta"]["row_counts"]["sleeve_curves"] = len(rows["sleeve_curves"]["series"])
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


MISSING_CREDS = (
    "Export did not refresh: SUPABASE_SERVICE_ROLE_KEY and SUPABASE_DB_URL are unset. "
    "Showing the last committed snapshot."
)


def parse_as_of(value) -> dt.datetime:
    text = str(value).strip().replace("Z", "+00:00")
    if " " in text and "T" not in text:
        text = text.replace(" ", "T", 1)
    parsed = dt.datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def latest_summary_rows(rows: list) -> list:
    """Keep the row with the greatest as_of for each sleeve.

    A history dump of kpi_sleeve_snapshots must not fail freshness on an
    older sibling, and must not be written into kpi_summary.json. A sleeve
    with no as_of keeps one row so assert_summary_fresh still fails loud.
    Empty input is unchanged.
    """
    if not rows:
        return list(rows)

    groups: dict[str, list] = {}
    order: list[str] = []
    for index, row in enumerate(rows):
        sleeve = row.get("sleeve") if isinstance(row, dict) else None
        if sleeve is None or str(sleeve).strip() == "":
            key = f"\0{index}"
        else:
            key = str(sleeve).strip().lower()
        if key not in groups:
            order.append(key)
            groups[key] = []
        groups[key].append(row)

    latest: list = []
    for key in order:
        cohort = groups[key]
        chosen = None
        chosen_at = None
        for row in cohort:
            as_of = row.get("as_of") if isinstance(row, dict) else None
            if not as_of:
                continue
            moment = parse_as_of(as_of)
            if chosen_at is None or moment >= chosen_at:
                chosen = row
                chosen_at = moment
        latest.append(cohort[0] if chosen is None else chosen)
    return latest


def assert_summary_fresh(rows: list, *, now: dt.datetime | None = None) -> None:
    """Refuse to write JSON when the latest sleeve snapshot is still pre-refresh.

    Pass rows from latest_summary_rows. Empty input, a missing as_of, or an
    as_of outside the window fails.
    """
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


def committed_as_of(target: Path) -> str | None:
    path = target / "kpi_summary.json"
    if not path.exists():
        return None
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(rows, list):
        return None
    best = None
    best_at = None
    for row in rows:
        if not isinstance(row, dict) or not row.get("as_of"):
            continue
        try:
            moment = parse_as_of(row["as_of"])
        except (TypeError, ValueError):
            continue
        if best_at is None or moment >= best_at:
            best_at = moment
            best = str(row["as_of"])
    return best


def failure_message(exc: BaseException) -> tuple[str, str]:
    """Public status for the board. The message must not contain secrets or KPI numbers."""
    text = str(exc).lower()
    errno = getattr(exc, "errno", None)
    if errno == 28 or "no space left" in text or "disk full" in text or "enospc" in text:
        return "error", "Export failed: disk full. KPI numbers were left unchanged."
    if errno == 30 or "read-only" in text or "readonly" in text or "read only" in text or "erofs" in text:
        return "error", "Export failed: read-only database or filesystem. KPI numbers were left unchanged."
    if "no supabase credential" in text:
        return "stale", MISSING_CREDS
    if isinstance(exc, RuntimeError) and str(exc).startswith("REST "):
        detail = str(exc)
        if "://" in detail or looks_like_secret(detail):
            detail = "REST read failed"
        else:
            parts = []
            for token in detail.split():
                if looks_like_secret(token) or token.startswith("eyJ"):
                    parts.append("[redacted]")
                else:
                    parts.append(token)
            detail = " ".join(parts)
        return "error", f"Export failed: {detail[:180]}. KPI numbers were left unchanged."
    return "error", "Export failed before it could refresh the snapshot. KPI numbers were left unchanged."


def classify_failure(exc: BaseException, frozen: str | None) -> tuple[str, str, str | None]:
    """Status, board copy, and warehouse_status. Does not invent mark-to-market."""
    status, message = failure_message(exc)
    raw = str(exc)
    low = f"{raw} {message}".lower()
    when = frozen or "the last committed snapshot"
    if "25006" in raw or "read-only" in low or "readonly" in low or "read only" in low:
        return "error", f"warehouse read-only — snapshot frozen at {when}", "read-only"
    if "53100" in raw or "disk full" in low or "no space" in low:
        return "error", f"warehouse disk full — snapshot frozen at {when}", "disk-full"
    if "stale after refresh" in low:
        return "error", raw[:300], "stale-snapshot"
    return status, message, None


def stamp_export_failure(
    target: Path,
    status: str,
    message: str,
    warehouse_status: str | None = None,
    snapshot_as_of: str | None = None,
) -> None:
    """Record a missed refresh on meta.json. Does not rewrite KPI JSON."""
    meta_path = target / "meta.json"
    meta: dict = {}
    if meta_path.exists():
        try:
            loaded = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            loaded = None
        if isinstance(loaded, dict):
            meta = loaded
    meta["export_status"] = status
    meta["export_error"] = message
    meta["export_attempted_at"] = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if warehouse_status:
        meta["warehouse_status"] = warehouse_status
    else:
        meta.pop("warehouse_status", None)
    if snapshot_as_of:
        meta["snapshot_as_of"] = snapshot_as_of
    write_json(meta_path, meta)


def write_bundle(target: Path, bundle: dict) -> None:
    for name in (*VIEWS, "meta"):
        write_json(target / f"{name}.json", bundle[name])
    if isinstance(bundle.get("open_positions"), dict):
        write_json(target / "open_positions.json", bundle["open_positions"])
    curves = bundle.get("sleeve_curves")
    if isinstance(curves, dict):
        write_json(target / "sleeve_curves.json", curves)
    elif isinstance(curves, list):
        write_json(target / "sleeve_curves.json", {"series": curves})
    oos = bundle.get("models_oos")
    if oos_has_rows(oos):
        if isinstance(oos, list):
            oos = {"rows": oos, "updated_at": bundle["meta"]["fetched_at"]}
        write_json(target / "models_oos.json", oos)


def self_test() -> int:
    """Mixed history passes; a cohort whose latest rows are all stale fails."""
    now = dt.datetime(2026, 9, 28, 1, 30, tzinfo=dt.timezone.utc)
    fresh = (now - dt.timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    stale = "2026-09-27T23:48:00Z"
    mixed = [
        {"sleeve": "crypto", "as_of": stale, "note": "old"},
        {"sleeve": "equities", "as_of": stale, "note": "old"},
        {"sleeve": "crypto", "as_of": fresh, "note": "new"},
        {"sleeve": "combined", "as_of": stale, "note": "old"},
        {"sleeve": "equities", "as_of": fresh, "note": "new"},
        {"sleeve": "combined", "as_of": fresh, "note": "new"},
        {"sleeve": "crypto"},
    ]
    latest = latest_summary_rows(mixed)
    if len(latest) != 3 or any(row.get("note") != "new" for row in latest):
        raise RuntimeError(f"latest-as_of filter kept the wrong rows: {latest}")
    assert_summary_fresh(latest, now=now)

    older = (now - dt.timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    all_stale = [
        {"sleeve": "crypto", "as_of": older},
        {"sleeve": "crypto", "as_of": stale},
        {"sleeve": "equities", "as_of": stale},
        {"sleeve": "combined", "as_of": stale},
    ]
    try:
        assert_summary_fresh(latest_summary_rows(all_stale), now=now)
    except RuntimeError as exc:
        if "stale" not in str(exc):
            raise
    else:
        raise RuntimeError("all-stale kpi_summary did not fail freshness")

    try:
        assert_summary_fresh([])
    except RuntimeError as exc:
        if "empty" not in str(exc):
            raise
    else:
        raise RuntimeError("empty kpi_summary did not fail")

    try:
        assert_summary_fresh(latest_summary_rows([{"sleeve": "crypto"}]), now=now)
    except RuntimeError as exc:
        if "as_of" not in str(exc):
            raise
    else:
        raise RuntimeError("missing as_of did not fail")

    long_why = "artifact buy " + ("x" * 400)
    fills = [
        {
            "sleeve": "crypto",
            "timestamp_et": "2026-09-27T18:00:00+00:00",
            "ticker": "W",
            "side": "sell",
            "pnl_trade_usd": "2.20",
            "why": "later",
        },
        {
            "sleeve": "crypto",
            "timestamp_et": "2026-09-26T16:00:00+00:00",
            "ticker": "QNT",
            "side": "sell",
            "pnl_trade_usd": "3.98",
            "why": long_why,
        },
        {
            "sleeve": "crypto",
            "timestamp_et": "2026-09-26T12:00:00+00:00",
            "ticker": "AVAX",
            "side": "buy",
            "pnl_trade_usd": "0",
            "why": "RH Agentic backfill order 6ab7f4a2-5444-4593-84ea-e78f57dc0cf6",
            "notes": "backfill from RH",
        },
        {
            "sleeve": "equities",
            "timestamp_et": "2026-09-25T19:37:00+00:00",
            "ticker": "QCOM",
            "side": "buy",
            "pnl_frac_of_book": 0,
            "why": "SWING unlock Joe/Wags; soft tgt flexible",
        },
    ]
    ledger = attach_running_ledger(fills)
    crypto = [row for row in ledger if row["sleeve"] == "crypto"]
    by_ticker = {row["ticker"]: row for row in crypto}
    if by_ticker["AVAX"]["running_pnl_frac"] != 0:
        raise RuntimeError(f"AVAX running pnl {by_ticker['AVAX']['running_pnl_frac']}")
    if by_ticker["QNT"]["running_pnl_frac"] != float(q6(Decimal("3.98") / Decimal("300"))):
        raise RuntimeError(f"QNT running pnl {by_ticker['QNT']['running_pnl_frac']}")
    expected_last = q6(Decimal("6.18") / Decimal("300"))
    if by_ticker["W"]["running_pnl_frac"] != float(expected_last):
        raise RuntimeError(f"W running pnl {by_ticker['W']['running_pnl_frac']}")
    if by_ticker["W"]["running_balance_frac"] != float(q6((Decimal("300") + Decimal("6.18")) / Decimal("300"))):
        raise RuntimeError(f"W running balance {by_ticker['W']['running_balance_frac']}")
    if by_ticker["W"]["running_pnl_frac"] is None or by_ticker["W"]["running_balance_frac"] is None:
        raise RuntimeError("last crypto fill ledger is null")
    if by_ticker["QNT"]["why"] != long_why:
        raise RuntimeError("why was truncated")
    if by_ticker["AVAX"]["why"] != "backfill from RH":
        raise RuntimeError(f"machine why was not replaced: {by_ticker['AVAX']['why']}")
    cleared = attach_running_ledger(
        [
            {
                "sleeve": "crypto",
                "timestamp_et": "2026-09-28T12:00:00+00:00",
                "ticker": "OP",
                "side": "buy",
                "pnl_trade_usd": "0",
                "why": None,
                "order_id": "6ab90000-0000-4000-8000-000000000004",
            }
        ]
    )
    if cleared[0].get("why") not in (None, ""):
        raise RuntimeError(f"cleared why was rewritten {cleared[0].get('why')!r}")
    if "sync order" in json.dumps(cleared):
        raise RuntimeError("export invented a sync-order why")
    scrubbed = scrub_row(cleared[0])
    if "order_id" in scrubbed or "order_id" not in DENY_KEYS:
        raise RuntimeError("order_id would be written to Pages JSON")
    equities = [row for row in ledger if row["sleeve"] == "equities"]
    if len(equities) != 1 or equities[0]["running_pnl_frac"] != 0 or equities[0]["running_balance_frac"] != 1:
        raise RuntimeError(f"equities ledger {equities}")
    # Scrubbed rows have no dollar P&L. Summing pnl_frac_of_book still fills the last row.
    bare = attach_running_ledger(
        [
            {"sleeve": "crypto", "timestamp_et": "2026-09-26T16:36:50+00:00", "ticker": "AVAX", "side": "buy", "pnl_frac_of_book": 0, "why": "a"},
            {"sleeve": "crypto", "timestamp_et": "2026-09-26T18:22:05+00:00", "ticker": "AVAX", "side": "sell", "pnl_frac_of_book": -0.001833, "why": "b"},
        ]
    )
    if bare[-1]["running_pnl_frac"] is None or bare[-1]["running_balance_frac"] is None:
        raise RuntimeError("frac-only ledger left the last row null")
    if bare[-1]["running_pnl_frac"] != float(q6(Decimal("-0.001833"))):
        raise RuntimeError(f"frac-only running pnl {bare[-1]['running_pnl_frac']}")

    opens = scrub_open_positions(
        [
            {
                "sleeve": "crypto",
                "ticker": "aaa",
                "side": "buy",
                "qty": "10",
                "avg_price": "2",
                "pnl_trade_usd": "99",
                "timestamp_et": "2026-09-01T00:00:00Z",
                "account_id": "546048042",
                "order_id": "6ab90000-0000-4000-8000-000000000099",
            },
            {
                "sleeve": "crypto",
                "ticker": "AAA",
                "side": "buy",
                "qty": "10",
                "avg_price": "4",
                "pnl_trade_usd": "0",
                "timestamp_et": "2026-09-02T00:00:00Z",
            },
            {
                "sleeve": "crypto",
                "ticker": "AAA",
                "side": "sell",
                "qty": "5",
                "avg_price": "5",
                "pnl_trade_usd": "10",
                "timestamp_et": "2026-09-03T00:00:00Z",
            },
            {
                "sleeve": "equities",
                "ticker": "QCOM",
                "side": "buy",
                "qty": "2",
                "avg_price": "100",
                "pnl_trade_usd": "0",
                "timestamp_et": "2026-09-04T00:00:00Z",
            },
        ],
        {("crypto", "AAA"): Decimal("4"), ("equities", "QCOM"): Decimal("110")},
        "2026-09-28T01:00:00Z",
    )
    by_ticker = {row["ticker"]: row for row in opens["positions"]}
    # 15 shares left at avg 3. Mark 4. Unrealized 15 / crypto seed 300.
    if by_ticker["AAA"]["side"] != "long" or by_ticker["AAA"]["qty"] != "15":
        raise RuntimeError(f"AAA open qty {by_ticker.get('AAA')}")
    if by_ticker["AAA"]["unrealized_pnl_frac"] != float(q6(Decimal("15") / Decimal("300"))):
        raise RuntimeError(f"AAA unrealized frac {by_ticker['AAA']['unrealized_pnl_frac']}")
    if by_ticker["QCOM"]["unrealized_pnl_frac"] != float(q6(Decimal("20") / Decimal("500"))):
        raise RuntimeError(f"QCOM unrealized frac {by_ticker['QCOM']['unrealized_pnl_frac']}")
    public_blob = json.dumps(opens)
    if "546048042" in public_blob or "unrealized_pnl_usd" in public_blob or "order_id" in public_blob:
        raise RuntimeError("open positions JSON leaked an account field")
    if "20.000000" in public_blob or "15.000000" in public_blob:
        raise RuntimeError("open positions JSON wrote raw unrealized dollars")
    try:
        scrub_open_positions(
            [
                {
                    "sleeve": "equities",
                    "ticker": "QCOM",
                    "side": "buy",
                    "qty": "1",
                    "avg_price": "10",
                    "pnl_trade_usd": "0",
                    "timestamp_et": "2026-09-01T00:00:00Z",
                }
            ],
            {},
            "2026-09-28T01:00:00Z",
        )
    except RuntimeError as exc:
        if "no mark" not in str(exc):
            raise
    else:
        raise RuntimeError("missing open mark did not fail")
    flat = scrub_open_positions(
        [
            {
                "sleeve": "crypto",
                "ticker": "W",
                "side": "buy",
                "qty": "3",
                "avg_price": "1",
                "pnl_trade_usd": "0",
                "timestamp_et": "2026-09-01T00:00:00Z",
            },
            {
                "sleeve": "crypto",
                "ticker": "W",
                "side": "sell",
                "qty": "3",
                "avg_price": "2",
                "pnl_trade_usd": "3",
                "timestamp_et": "2026-09-02T00:00:00Z",
            },
        ],
        {},
        "2026-09-28T01:00:00Z",
    )
    if flat["positions"]:
        raise RuntimeError("a flat book should not invent an open position")

    history = scrub_snapshot_history(
        [
            {
                "sleeve": "crypto",
                "as_of": "2026-09-28T00:00:00Z",
                "running_balance_usd": "323.77",
                "running_pnl_usd": "23.77",
                "realized_pnl_usd": "6.24",
                "unrealized_pnl_usd": "17.53",
                "start_balance_usd": "300",
                "account_id": "546048042",
                "notes": "realized $6.24; book $323.77",
            },
            {
                "sleeve": "equities",
                "as_of": "2026-09-27T00:00:00Z",
                "running_pnl_frac": "0.001740",
            },
            {
                "sleeve": "combined",
                "as_of": "2026-09-28T00:00:00Z",
                "running_balance_usd": "824.64",
                "email": "joe@example.com",
            },
        ]
    )
    history_blob = json.dumps(history)
    if "323.77" in history_blob or "546048042" in history_blob or "joe@example.com" in history_blob:
        raise RuntimeError(f"curve JSON leaked warehouse dollars or an account: {history_blob}")
    if "$" in history_blob or "running_balance_usd" in history_blob or "notes" in history_blob:
        raise RuntimeError("curve JSON kept a dollar column or a note")
    crypto_point = next(row for row in history if row["sleeve"] == "crypto")
    if crypto_point["running_balance_frac"] != float(q6(Decimal("323.77") / Decimal("300"))):
        raise RuntimeError(f"crypto curve frac {crypto_point['running_balance_frac']}")
    equities_point = next(row for row in history if row["sleeve"] == "equities")
    if equities_point["running_balance_frac"] != float(q6(Decimal("1") + Decimal("0.001740"))):
        raise RuntimeError("equities curve did not derive book from running P&L")
    if [row["sleeve"] for row in history] != ["equities", "combined", "crypto"]:
        raise RuntimeError(f"curve order {history}")

    print("self-test ok")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--install-sample",
        action="store_true",
        help="Write the committed sample into data/ and fixtures/ and exit",
    )
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="Check latest-as_of filtering against the freshness window, then exit",
    )
    args = parser.parse_args(argv)

    if args.self_test:
        return self_test()

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
            try:
                stamp_export_failure(DATA, "error", MISSING_CREDS)
            except OSError as exc:
                print(f"Could not record export status in data/meta.json: {exc}", file=sys.stderr)
            return 1
        print(MISSING_CREDS)
        needed = [DATA / f"{name}.json" for name in (*VIEWS, *OPTIONAL_VIEWS, "models", "meta")]
        if any(not path.exists() for path in needed):
            print("Sample JSON missing; installing fixtures.", file=sys.stderr)
            install_sample(DATA)
        try:
            stamp_export_failure(DATA, "stale", MISSING_CREDS)
        except OSError as exc:
            print(f"Could not record export status in data/meta.json: {exc}", file=sys.stderr)
            return 1
        return 0

    try:
        bundle = export_live(base_url, key, db_url)
        if refresh_expected:
            assert_summary_fresh(bundle["kpi_summary"])
        write_bundle(DATA, bundle)
    except Exception as exc:
        frozen = committed_as_of(DATA)
        status, message, warehouse_status = classify_failure(exc, frozen)
        print(message, file=sys.stderr)
        try:
            stamp_export_failure(
                DATA,
                status,
                message,
                warehouse_status=warehouse_status,
                snapshot_as_of=frozen,
            )
        except OSError as stamp_exc:
            print(f"Could not record export status in data/meta.json: {stamp_exc}", file=sys.stderr)
        return 1
    if not (DATA / "models.json").exists():
        write_json(DATA / "models.json", sample_bundle()["models"])
    print(
        "Exported "
        + ", ".join(f"{name}={bundle['meta']['row_counts'][name]}" for name in VIEWS)
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
