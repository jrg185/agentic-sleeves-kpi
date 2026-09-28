#!/usr/bin/env python3
"""Insert filled Robinhood crypto orders into public.kpi_trades.

Export KPI already refreshes sleeve snapshots and exports scrubbed JSON.
This script is the fill ingest in front of that refresh. It only signs GET
requests against the Robinhood Crypto Trading API. It does not place, cancel,
or preview orders.

jrg185/agentic-crypto-signals has no Robinhood client (it is not a broker).
This repo did not document RH secrets either. Conventional names:

  RH_API_KEY               Crypto Trading API key. Sent as x-api-key.
                           Create it on the Agentic crypto account.
  RH_BASE64_PRIVATE_KEY    Base64 Ed25519 private-key seed. Used only to sign
                           GET /api/v1/crypto/trading/orders/ (or v2 when
                           RH_ACCOUNT_NUMBER is set). Never printed.
  RH_ACCOUNT_NUMBER        Optional. When set, list v2 orders for that account.

Supabase, same as refresh and export:

  SUPABASE_URL
  SUPABASE_SERVICE_ROLE_KEY
  SUPABASE_DB_URL          Optional Postgres URL when REST cannot read or insert.

A live run with RH_API_KEY or RH_BASE64_PRIVATE_KEY unset exits 1 and stamps
data/meta.json. It does not exit 0.

Cursor: data/rh_kpi_sync_cursor.json when that file exists, otherwise
max(timestamp_et) on the crypto sleeve, otherwise 2026-09-01. The query uses
updated_at_start six hours before that watermark. Rows already stored with
`RH Agentic sync order <uuid>` or `RH Agentic backfill order <uuid>` are not
inserted again.

USDC and USDC pairs are skipped. Buys store pnl_trade_usd 0. A closing leg
stores price P&L against the open average so a later snapshot does not drop
the gain. Fee stays in fee_usd. Equities are mapped when an order payload
says asset_class equity; the live fetch is crypto only (this API has no
equity orders route).

  python3 scripts/sync_rh_kpi_trades.py --self-test
  python3 scripts/sync_rh_kpi_trades.py --dry-run
  python3 scripts/sync_rh_kpi_trades.py

--dry-run maps fixture orders and does not call Robinhood or Supabase.
Signing uses pynacl when it is installed, and cryptography otherwise.
The Export KPI job installs pynacl before the live run.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
CURSOR_PATH = DATA / "rh_kpi_sync_cursor.json"
DEFAULT_URL = "https://bsnqwgbshwszbjncglqx.supabase.co"
RH_BASE = "https://trading.robinhood.com"
BOOTSTRAP_CURSOR = "2026-09-01T00:00:00Z"
OVERLAP = dt.timedelta(hours=6)
DUST = Decimal("0.00000001")
PAGE_CAP = 50
SYMBOL = re.compile(r"^[A-Z0-9]{1,15}$")
UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
WHY_SYNC = "RH Agentic sync order {order_id}"
USER_AGENT = "the-book-rh-kpi-sync/1"
WRITE_COLUMNS = (
    "sleeve",
    "timestamp_et",
    "ticker",
    "side",
    "qty",
    "avg_price",
    "notional_usd",
    "fee_usd",
    "pnl_trade_usd",
    "why",
)

# Public example from https://docs.robinhood.com/crypto/trading/ (not a live key).
DOC_API_KEY = "rh-api-6148effc-c0b1-486c-8940-a1d099456be6"
DOC_PRIVATE_KEY = "xQnTJVeQLmw1/Mg2YimEViSpw/SdJcgNXZ5kQkAXNPU="
DOC_TIMESTAMP = "1698708981"
DOC_PATH = "/api/v1/crypto/trading/orders/"
DOC_SIGNATURE = "q/nEtxp/P2Or3hph3KejBqnw5o9qeuQ+hYRnB56FaHbjDsNUY9KhB1asMxohDnzdVFSD7StaTqjSd9U9HvaRAw=="

MISSING_RH = (
    "RH fill sync needs GitHub Actions secrets RH_API_KEY and RH_BASE64_PRIVATE_KEY "
    "(Robinhood Crypto Trading API key and base64 Ed25519 private key, from the Agentic "
    "crypto account). Optional RH_ACCOUNT_NUMBER selects the v2 orders account. "
    "No orders were read and kpi_trades was not changed."
)
MISSING_SB = (
    "RH fill sync needs SUPABASE_SERVICE_ROLE_KEY or SUPABASE_DB_URL. "
    "kpi_trades was not changed."
)


class SyncError(RuntimeError):
    """A live sync that must not exit 0."""


def dec(value) -> Decimal | None:
    if value is None or value == "":
        return None
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def num_text(value: Decimal) -> str:
    quantized = value.quantize(Decimal("0.00000001"))
    text = format(quantized, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def parse_ts(value) -> dt.datetime:
    if isinstance(value, dt.datetime):
        moment = value
    else:
        text = str(value).strip().replace("Z", "+00:00")
        moment = dt.datetime.fromisoformat(text)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.timezone.utc)
    return moment


def iso_utc(value) -> str:
    return parse_ts(value).astimezone(dt.timezone.utc).isoformat()


def iso_z(value) -> str:
    return parse_ts(value).astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def uuids_in(text: str) -> set[str]:
    return {match.group(0).lower() for match in UUID_RE.finditer(text or "")}


def order_id_from_why(why: str) -> str:
    found = uuids_in(why)
    if len(found) != 1:
        raise SyncError("why text is missing its Robinhood order id")
    return next(iter(found))


def known_order_ids(whys: list[str]) -> set[str]:
    found: set[str] = set()
    for why in whys:
        found.update(uuids_in(why))
    return found


def split_symbol(order: dict) -> tuple[str, str]:
    code = str(order.get("currency_code") or "").strip().upper()
    symbol = str(order.get("symbol") or order.get("currency_pair") or "").strip().upper()
    quote = ""
    base = symbol
    if "-" in symbol:
        base, _, quote = symbol.partition("-")
    if code:
        return code, quote
    return base, quote


def is_usdc(ticker: str, quote: str) -> bool:
    return ticker == "USDC" or quote == "USDC"


def sleeve_of(order: dict) -> str:
    asset = str(order.get("asset_class") or order.get("instrument_type") or "").strip().lower()
    if asset in {"equity", "stock", "equities"}:
        return "equities"
    return "crypto"


def fee_of(order: dict) -> Decimal:
    direct = dec(order.get("fee"))
    if direct is not None:
        return direct
    charged = dec(order.get("fee_charged"))
    if charged is not None:
        return charged
    total = Decimal("0")
    fees = order.get("fees")
    if isinstance(fees, list):
        for item in fees:
            if not isinstance(item, dict):
                continue
            data = item.get("fee_data")
            if isinstance(data, dict):
                amount = dec(data.get("fee_amount"))
                if amount is not None:
                    total += amount
    return total


def qty_of(order: dict) -> Decimal | None:
    for key in ("cumulative_quantity", "filled_asset_quantity", "quantity"):
        amount = dec(order.get(key))
        if amount is not None:
            return amount
    return None


def notional_of(order: dict, qty: Decimal, price: Decimal) -> Decimal:
    for key in ("rounded_executed_notional", "total_executed_notional", "executed_notional"):
        amount = dec(order.get(key))
        if amount is not None:
            return amount
    return qty * price


def fill_time(order: dict) -> str:
    executions = order.get("executions")
    stamps: list[str] = []
    if isinstance(executions, list):
        for item in executions:
            if isinstance(item, dict) and item.get("timestamp"):
                stamps.append(str(item["timestamp"]))
    if stamps:
        latest = max(stamps, key=parse_ts)
        return iso_utc(latest)
    for key in ("created_at", "updated_at"):
        if order.get(key):
            return iso_utc(order[key])
    raise SyncError("filled order is missing a timestamp")


def order_updated_at(order: dict) -> dt.datetime | None:
    for key in ("updated_at", "created_at"):
        if order.get(key):
            return parse_ts(order[key])
    return None


def map_order(order: dict) -> dict | None:
    """Map one filled order to a kpi_trades row. USDC and non-fills return None."""
    if not isinstance(order, dict):
        return None
    state = str(order.get("state") or order.get("derived_state") or "").strip().lower()
    if state != "filled":
        return None
    order_id = str(order.get("id") or "").strip().lower()
    if not UUID_RE.fullmatch(order_id):
        raise SyncError("filled order is missing an id")
    ticker, quote = split_symbol(order)
    if is_usdc(ticker, quote):
        return None
    if not SYMBOL.fullmatch(ticker):
        raise SyncError(f"filled order {order_id} ticker {ticker!r} is not a mark symbol")
    side = str(order.get("side") or "").strip().lower()
    if side not in {"buy", "sell"}:
        raise SyncError(f"filled order {order_id} side {side!r} is not buy or sell")
    qty = qty_of(order)
    price = dec(order.get("average_price"))
    if qty is None or qty <= 0:
        raise SyncError(f"filled order {order_id} has no positive quantity")
    if price is None or price <= 0:
        raise SyncError(f"filled order {order_id} has no positive average_price")
    sleeve = sleeve_of(order)
    row = {
        "sleeve": sleeve,
        "timestamp_et": fill_time(order),
        "ticker": ticker,
        "side": side,
        "qty": num_text(qty),
        "avg_price": num_text(price),
        "notional_usd": num_text(notional_of(order, qty, price)),
        "fee_usd": num_text(fee_of(order)),
        "pnl_trade_usd": "0",
        "why": WHY_SYNC.format(order_id=order_id),
    }
    return row


def map_orders(orders: list[dict]) -> list[dict]:
    rows = []
    for order in orders:
        row = map_order(order)
        if row is not None:
            rows.append(row)
    return rows


def drop_known(rows: list[dict], existing_whys: list[str]) -> list[dict]:
    """Keep the first row for each Robinhood order id. Existing whys win."""
    known = known_order_ids(existing_whys)
    kept = []
    for row in rows:
        order_id = order_id_from_why(row["why"])
        if order_id in known:
            continue
        known.add(order_id)
        kept.append(row)
    return kept


def apply_fill(book: dict, row: dict, assign: bool) -> None:
    sleeve = row["sleeve"]
    ticker = row["ticker"]
    side = row["side"]
    qty = dec(row["qty"])
    price = dec(row["avg_price"])
    if qty is None or qty <= 0 or price is None or price <= 0:
        raise SyncError(f"{sleeve} {ticker}: qty and avg_price must be positive")
    signed = qty if side == "buy" else -qty
    key = (sleeve, ticker)
    pos = book.get(key)
    open_qty = pos["qty"] if pos else Decimal("0")
    increasing = (signed > 0 and open_qty >= 0) or (signed < 0 and open_qty <= 0)
    if increasing:
        new_qty = open_qty + signed
        if abs(open_qty) <= DUST:
            avg = price
        else:
            avg = (abs(open_qty) * pos["avg"] + abs(signed) * price) / abs(new_qty)
        book[key] = {"qty": new_qty, "avg": avg}
        if assign:
            row["pnl_trade_usd"] = "0"
        return
    if abs(signed) - abs(open_qty) > DUST:
        raise SyncError(f"{sleeve} {ticker}: close qty {qty} exceeds open {abs(open_qty)}")
    if assign:
        if open_qty > 0:
            pnl = (price - pos["avg"]) * qty
        else:
            pnl = (pos["avg"] - price) * qty
        row["pnl_trade_usd"] = num_text(pnl)
    remain = abs(open_qty) - abs(signed)
    if remain <= DUST:
        book.pop(key, None)
    else:
        sign = Decimal("1") if open_qty > 0 else Decimal("-1")
        book[key] = {"qty": sign * remain, "avg": pos["avg"]}


def assign_pnl(existing: list[dict], new_rows: list[dict]) -> list[dict]:
    """Replay the book. Opening buys stay at pnl 0. Closing legs get price P&L."""
    events: list[tuple[str, dict]] = [("old", row) for row in existing]
    events.extend(("new", row) for row in new_rows)
    events.sort(key=lambda item: (parse_ts(item[1]["timestamp_et"]), 0 if item[0] == "old" else 1))
    book: dict = {}
    for kind, row in events:
        apply_fill(book, row, assign=(kind == "new"))
    return new_rows


def rows_from_orders(orders: list[dict], existing: list[dict]) -> list[dict]:
    mapped = map_orders(orders)
    fresh = drop_known(mapped, [str(row.get("why") or "") for row in existing])
    return assign_pnl(existing, fresh)


def sign_message(private_key_b64: str, message: str) -> str:
    try:
        seed = base64.b64decode(private_key_b64, validate=True)
    except Exception as exc:
        raise SyncError("RH_BASE64_PRIVATE_KEY is not valid base64") from exc
    if len(seed) == 64:
        seed = seed[:32]
    if len(seed) != 32:
        raise SyncError("RH_BASE64_PRIVATE_KEY is not a 32-byte Ed25519 seed")
    signature = None
    try:
        from nacl.signing import SigningKey

        signature = SigningKey(seed).sign(message.encode("utf-8")).signature
    except ImportError:
        signature = None
    if signature is None:
        try:
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

            signature = Ed25519PrivateKey.from_private_bytes(seed).sign(message.encode("utf-8"))
        except ImportError as exc:
            raise SyncError(
                "RH fill sync needs pynacl or cryptography to sign GET requests. "
                "pip install pynacl"
            ) from exc
    return base64.b64encode(signature).decode("utf-8")


def request_message(api_key: str, timestamp: str, path: str, method: str, body: str = "") -> str:
    return f"{api_key}{timestamp}{path}{method}{body}"


def rh_get(api_key: str, private_key_b64: str, path: str) -> dict:
    if not path.startswith("/") or path.startswith("//"):
        raise SyncError("refusing a Robinhood path that is not on trading.robinhood.com")
    timestamp = str(int(dt.datetime.now(dt.timezone.utc).timestamp()))
    message = request_message(api_key, timestamp, path, "GET")
    signature = sign_message(private_key_b64, message)
    request = urllib.request.Request(
        RH_BASE + path,
        headers={
            "x-api-key": api_key,
            "x-signature": signature,
            "x-timestamp": timestamp,
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=40) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        raise SyncError(f"Robinhood GET orders failed with HTTP {exc.code}. kpi_trades was not changed.") from None
    except (urllib.error.URLError, TimeoutError) as exc:
        raise SyncError("Robinhood GET orders failed: network error. kpi_trades was not changed.") from exc
    try:
        payload = json.loads(raw.decode("utf-8")) if raw else None
    except json.JSONDecodeError as exc:
        raise SyncError("Robinhood GET orders returned non-JSON. kpi_trades was not changed.") from exc
    if not isinstance(payload, dict):
        raise SyncError("Robinhood GET orders returned an unexpected payload. kpi_trades was not changed.")
    return payload


def path_from_next(next_url: str) -> str:
    if next_url.startswith(RH_BASE):
        path = next_url[len(RH_BASE) :]
    else:
        parsed = urllib.parse.urlparse(next_url)
        if parsed.scheme or parsed.netloc:
            raise SyncError("Robinhood pagination left trading.robinhood.com")
        path = parsed.path
        if parsed.query:
            path = f"{path}?{parsed.query}"
    if not path.startswith("/api/"):
        raise SyncError("Robinhood pagination path was not an orders path")
    return path


def orders_path(account: str, updated_at_start: str) -> str:
    if account:
        if not re.fullmatch(r"[A-Za-z0-9]+", account):
            raise SyncError("RH_ACCOUNT_NUMBER has unexpected characters")
        return (
            "/api/v2/crypto/trading/orders/"
            f"?account_number={account}&state=filled&updated_at_start={updated_at_start}&limit=100"
        )
    return f"/api/v1/crypto/trading/orders/?state=filled&updated_at_start={updated_at_start}&limit=100"


def fetch_filled_orders(api_key: str, private_key_b64: str, account: str, updated_at_start: str) -> list[dict]:
    path = orders_path(account, updated_at_start)
    orders: list[dict] = []
    seen: set[str] = set()
    for _ in range(PAGE_CAP):
        if path in seen:
            break
        seen.add(path)
        payload = rh_get(api_key, private_key_b64, path)
        batch = payload.get("results")
        if not isinstance(batch, list):
            raise SyncError("Robinhood orders response had no results list. kpi_trades was not changed.")
        orders.extend(item for item in batch if isinstance(item, dict))
        nxt = payload.get("next")
        if not nxt:
            return orders
        path = path_from_next(str(nxt))
    raise SyncError("Robinhood orders pagination exceeded 50 pages. kpi_trades was not changed.")


def read_cursor(path: Path) -> str | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or not payload.get("updated_at"):
        return None
    try:
        return iso_z(payload["updated_at"])
    except (TypeError, ValueError):
        return None


def write_cursor(path: Path, updated_at: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"updated_at": iso_z(updated_at), "source": "rh-crypto-trading-api"}
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def query_start(cursor: str) -> str:
    return iso_z(parse_ts(cursor) - OVERLAP)


def latest_timestamp(rows: list[dict], sleeve: str) -> str | None:
    moments = []
    for row in rows:
        if str(row.get("sleeve") or "").lower() != sleeve:
            continue
        if row.get("timestamp_et"):
            moments.append(parse_ts(row["timestamp_et"]))
    if not moments:
        return None
    return iso_z(max(moments))


def resolve_cursor(existing: list[dict], cursor_path: Path) -> str:
    stored = read_cursor(cursor_path)
    if stored:
        return stored
    latest = latest_timestamp(existing, "crypto")
    return latest or BOOTSTRAP_CURSOR


def advance_cursor(previous: str, orders: list[dict]) -> str | None:
    moments = [stamp for order in orders if (stamp := order_updated_at(order))]
    if not moments:
        return None
    latest = max(moments)
    if parse_ts(previous) > latest:
        return previous
    return iso_z(latest)


def rest_headers(key: str) -> dict[str, str]:
    return {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    }


def rest_call(base_url: str, key: str, path: str, method: str = "GET", body=None, extra_headers=None):
    data = None if body is None else json.dumps(body).encode("utf-8")
    headers = rest_headers(key)
    if body is not None:
        headers["Content-Type"] = "application/json"
    if extra_headers:
        headers.update(extra_headers)
    request = urllib.request.Request(
        base_url.rstrip("/") + path,
        data=data,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=40) as response:
            raw = response.read()
            return json.loads(raw.decode("utf-8")) if raw else None
    except urllib.error.HTTPError as exc:
        raise SyncError(f"REST {method} kpi_trades failed with HTTP {exc.code}. kpi_trades was not changed.") from None
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise SyncError("REST kpi_trades failed: network error. kpi_trades was not changed.") from exc


def fetch_trades_rest(base_url: str, key: str) -> list[dict]:
    rows: list[dict] = []
    page = 1000
    select = "sleeve,ticker,side,qty,avg_price,timestamp_et,why,pnl_trade_usd"
    for offset in range(0, page * 20, page):
        path = f"/rest/v1/kpi_trades?select={select}&order=timestamp_et.asc&limit={page}&offset={offset}"
        payload = rest_call(base_url, key, path)
        if not isinstance(payload, list):
            raise SyncError("REST kpi_trades did not return a row list")
        rows.extend(row for row in payload if isinstance(row, dict))
        if len(payload) < page:
            return rows
    raise SyncError("REST kpi_trades exceeded 20000 rows; refusing a partial sync")


def insert_rest(base_url: str, key: str, rows: list[dict]) -> None:
    payload = [{column: row[column] for column in WRITE_COLUMNS} for row in rows]
    rest_call(
        base_url,
        key,
        "/rest/v1/kpi_trades",
        method="POST",
        body=payload,
        extra_headers={"Prefer": "return=minimal"},
    )


def connect_db(db_url: str):
    try:
        import psycopg
    except ImportError as exc:
        raise SyncError("psycopg is required for SUPABASE_DB_URL") from exc
    return psycopg.connect(db_url, connect_timeout=20)


def fetch_trades_db(db_url: str) -> list[dict]:
    sql = """
        select sleeve, ticker, side, qty, avg_price, timestamp_et, why, pnl_trade_usd
        from public.kpi_trades
        order by timestamp_et asc
    """
    try:
        with connect_db(db_url) as conn:
            with conn.cursor() as cur:
                cur.execute(sql)
                columns = [desc.name for desc in cur.description]
                return [dict(zip(columns, row)) for row in cur.fetchall()]
    except SyncError:
        raise
    except Exception as exc:
        raise SyncError("database read of kpi_trades failed. kpi_trades was not changed.") from exc


def insert_db(db_url: str, rows: list[dict]) -> None:
    sql = """
        insert into public.kpi_trades (
            sleeve, timestamp_et, ticker, side, qty, avg_price,
            notional_usd, fee_usd, pnl_trade_usd, why
        ) values (
            %(sleeve)s, %(timestamp_et)s, %(ticker)s, %(side)s, %(qty)s, %(avg_price)s,
            %(notional_usd)s, %(fee_usd)s, %(pnl_trade_usd)s, %(why)s
        )
    """
    payload = [{column: row[column] for column in WRITE_COLUMNS} for row in rows]
    try:
        with connect_db(db_url) as conn:
            with conn.cursor() as cur:
                cur.executemany(sql, payload)
            conn.commit()
    except SyncError:
        raise
    except Exception as exc:
        raise SyncError("database insert into kpi_trades failed. The sync did not finish.") from exc


def load_trades(env: dict[str, str]) -> tuple[list[dict], str]:
    key = env.get("SUPABASE_SERVICE_ROLE_KEY") or ""
    db_url = env.get("SUPABASE_DB_URL") or ""
    base_url = env.get("SUPABASE_URL") or DEFAULT_URL
    if key:
        try:
            return fetch_trades_rest(base_url, key), "rest"
        except SyncError:
            if not db_url:
                raise
            print("REST read failed; trying SUPABASE_DB_URL", file=sys.stderr)
    if not db_url:
        raise SyncError(MISSING_SB)
    return fetch_trades_db(db_url), "db"


def insert_rows(env: dict[str, str], rows: list[dict], source: str) -> None:
    if not rows:
        return
    key = env.get("SUPABASE_SERVICE_ROLE_KEY") or ""
    db_url = env.get("SUPABASE_DB_URL") or ""
    base_url = env.get("SUPABASE_URL") or DEFAULT_URL
    if key and source == "rest":
        try:
            insert_rest(base_url, key, rows)
            return
        except SyncError:
            if not db_url:
                raise
            print("REST insert failed; trying SUPABASE_DB_URL", file=sys.stderr)
    if not db_url:
        raise SyncError(MISSING_SB)
    insert_db(db_url, rows)


def env_values() -> dict[str, str]:
    names = (
        "SUPABASE_URL",
        "SUPABASE_SERVICE_ROLE_KEY",
        "SUPABASE_DB_URL",
        "RH_API_KEY",
        "RH_BASE64_PRIVATE_KEY",
        "RH_ACCOUNT_NUMBER",
    )
    return {name: (os.environ.get(name) or "").strip() for name in names}


def require_rh(env: dict[str, str]) -> tuple[str, str, str]:
    api_key = env.get("RH_API_KEY") or ""
    private_key = env.get("RH_BASE64_PRIVATE_KEY") or ""
    account = env.get("RH_ACCOUNT_NUMBER") or ""
    if not api_key or not private_key:
        raise SyncError(MISSING_RH)
    return api_key, private_key, account


def public_sync_message(message: str) -> str:
    text = message
    for name in (
        "RH_API_KEY",
        "RH_BASE64_PRIVATE_KEY",
        "RH_ACCOUNT_NUMBER",
        "SUPABASE_SERVICE_ROLE_KEY",
        "SUPABASE_DB_URL",
    ):
        secret = (os.environ.get(name) or "").strip()
        if len(secret) >= 6:
            text = text.replace(secret, "[redacted]")
    lowered = text.lower()
    if "read-only" in lowered or "readonly" in lowered or "disk full" in lowered or "25006" in text:
        text = "RH fill sync failed. kpi_trades was not changed."
    return text[:500]


def stamp_sync_failure(message: str, target: Path | None = None) -> None:
    """Record the miss on meta.json. Does not rewrite KPI numbers."""
    sys.path.insert(0, str(ROOT / "scripts"))
    import export_kpi

    export_kpi.stamp_export_failure(target or DATA, "error", public_sync_message(message))


def sync(cursor_path: Path = CURSOR_PATH) -> int:
    env = env_values()
    api_key, private_key, account = require_rh(env)
    if not (env.get("SUPABASE_SERVICE_ROLE_KEY") or env.get("SUPABASE_DB_URL")):
        raise SyncError(MISSING_SB)
    existing, source = load_trades(env)
    cursor = resolve_cursor(existing, cursor_path)
    start = query_start(cursor)
    orders = fetch_filled_orders(api_key, private_key, account, start)
    fresh = rows_from_orders(orders, existing)
    insert_rows(env, fresh, source)
    nxt = advance_cursor(cursor, orders)
    if nxt:
        write_cursor(cursor_path, nxt)
    print(
        f"rh sync source={source} fetched={len(orders)} inserted={len(fresh)} "
        f"cursor={nxt or cursor}"
    )
    return 0


def fixture_orders() -> list[dict]:
    return [
        {
            "id": "11111111-1111-4111-8111-111111111111",
            "currency_code": "AAA",
            "side": "buy",
            "state": "filled",
            "cumulative_quantity": "10",
            "average_price": "2",
            "rounded_executed_notional": "20",
            "fee": "0.10",
            "created_at": "2026-09-28T10:00:00Z",
            "updated_at": "2026-09-28T10:00:01Z",
        },
        {
            "id": "22222222-2222-4222-8222-222222222222",
            "currency_code": "AAA",
            "side": "sell",
            "state": "filled",
            "cumulative_quantity": "4",
            "average_price": "5",
            "fee": "0.20",
            "created_at": "2026-09-28T11:00:00Z",
            "updated_at": "2026-09-28T11:00:01Z",
        },
        {
            "id": "33333333-3333-4333-8333-333333333333",
            "currency_code": "USDC",
            "side": "buy",
            "state": "filled",
            "cumulative_quantity": "25",
            "average_price": "1",
            "created_at": "2026-09-28T11:30:00Z",
        },
        {
            "id": "44444444-4444-4444-8444-444444444444",
            "symbol": "BTC-USDC",
            "side": "buy",
            "state": "filled",
            "filled_asset_quantity": "0.01",
            "average_price": "100",
            "created_at": "2026-09-28T11:40:00Z",
        },
        {
            "id": "55555555-5555-4555-8555-555555555555",
            "symbol": "BTC-USD",
            "side": "buy",
            "state": "filled",
            "filled_asset_quantity": "0.01",
            "average_price": "100",
            "fee_charged": "0.25",
            "created_at": "2026-09-28T12:00:00Z",
            "executions": [{"effective_price": "100", "quantity": "0.01", "timestamp": "2026-09-28T12:00:02Z"}],
        },
        {
            "id": "66666666-6666-4666-8666-666666666666",
            "symbol": "ETH-USD",
            "side": "buy",
            "state": "canceled",
            "filled_asset_quantity": "1",
            "average_price": "10",
            "created_at": "2026-09-28T12:30:00Z",
        },
        {
            "id": "77777777-7777-4777-8777-777777777777",
            "asset_class": "equity",
            "symbol": "QCOM",
            "side": "buy",
            "state": "filled",
            "cumulative_quantity": "1",
            "average_price": "100",
            "created_at": "2026-09-28T15:00:00Z",
        },
        {
            "id": "22222222-2222-4222-8222-222222222222",
            "currency_code": "AAA",
            "side": "sell",
            "state": "filled",
            "cumulative_quantity": "4",
            "average_price": "5",
            "created_at": "2026-09-28T11:00:00Z",
        },
    ]


def fixture_rows(existing: list[dict] | None = None) -> list[dict]:
    prior = existing if existing is not None else []
    return rows_from_orders(fixture_orders(), prior)


def signing_available() -> bool:
    try:
        import nacl.signing  # noqa: F401

        return True
    except ImportError:
        pass
    try:
        import cryptography.hazmat.primitives.asymmetric.ed25519  # noqa: F401

        return True
    except ImportError:
        return False


def self_test() -> int:
    existing = [
        {
            "sleeve": "crypto",
            "ticker": "AAA",
            "side": "buy",
            "qty": "10",
            "avg_price": "2",
            "timestamp_et": "2026-09-28T09:00:00+00:00",
            "why": "RH Agentic backfill order 11111111-1111-4111-8111-111111111111",
            "pnl_trade_usd": "0",
        }
    ]
    rows = fixture_rows(existing)
    by_id = {order_id_from_why(row["why"]): row for row in rows}
    if "11111111-1111-4111-8111-111111111111" in by_id:
        raise SyncError("backfill order id was inserted again")
    if "33333333-3333-4333-8333-333333333333" in by_id or "44444444-4444-4444-8444-444444444444" in by_id:
        raise SyncError("USDC order was not skipped")
    if "66666666-6666-4666-8666-666666666666" in by_id:
        raise SyncError("canceled order was inserted")
    sell = by_id["22222222-2222-4222-8222-222222222222"]
    if sell["why"] != "RH Agentic sync order 22222222-2222-4222-8222-222222222222":
        raise SyncError(f"why {sell['why']}")
    if sell["sleeve"] != "crypto" or sell["ticker"] != "AAA" or sell["side"] != "sell":
        raise SyncError("sell shape")
    if sell["qty"] != "4" or sell["avg_price"] != "5" or sell["notional_usd"] != "20":
        raise SyncError(f"sell numbers {sell}")
    if sell["fee_usd"] != "0.2" or sell["pnl_trade_usd"] != "12":
        raise SyncError(f"sell fee/pnl {sell['fee_usd']} {sell['pnl_trade_usd']}")
    if rows.count(sell) != 1:
        raise SyncError("duplicate sell in one batch was inserted twice")
    btc = by_id["55555555-5555-4555-8555-555555555555"]
    if btc["ticker"] != "BTC" or btc["qty"] != "0.01" or btc["pnl_trade_usd"] != "0":
        raise SyncError("BTC buy shape")
    if btc["fee_usd"] != "0.25" or btc["notional_usd"] != "1":
        raise SyncError("BTC fee/notional")
    if btc["timestamp_et"] != "2026-09-28T12:00:02+00:00":
        raise SyncError(f"execution timestamp {btc['timestamp_et']}")
    qcom = by_id["77777777-7777-4777-8777-777777777777"]
    if qcom["sleeve"] != "equities" or qcom["ticker"] != "QCOM" or qcom["pnl_trade_usd"] != "0":
        raise SyncError("equity buy shape")
    again = drop_known(rows, [sell["why"], btc["why"]])
    if any(order_id_from_why(row["why"]) in {sell_id(sell), sell_id(btc)} for row in again):
        raise SyncError("second pass inserted a known order id")
    message = request_message(DOC_API_KEY, DOC_TIMESTAMP, DOC_PATH, "GET")
    if message != f"{DOC_API_KEY}{DOC_TIMESTAMP}{DOC_PATH}GET":
        raise SyncError("GET signature message")
    if "place" in message.lower():
        raise SyncError("signature message must be a GET")
    if signing_available():
        body = str(
            {
                "client_order_id": "131de903-5a9c-4260-abc1-28d562a5dcf0",
                "side": "buy",
                "symbol": "BTC-USD",
                "type": "market",
                "market_order_config": {"asset_quantity": "0.1"},
            }
        )
        signed = sign_message(
            DOC_PRIVATE_KEY,
            request_message(DOC_API_KEY, DOC_TIMESTAMP, DOC_PATH, "POST", body),
        )
        if signed != DOC_SIGNATURE:
            raise SyncError("documented Ed25519 signature did not match")
    else:
        print("signature vector skipped (install pynacl or cryptography)", file=sys.stderr)
    if "read-only" in MISSING_RH.lower() or "disk full" in MISSING_RH.lower():
        raise SyncError("missing-secret copy must not look like a warehouse outage")
    try:
        require_rh({})
    except SyncError as exc:
        if "RH_API_KEY" not in str(exc) or "RH_BASE64_PRIVATE_KEY" not in str(exc):
            raise
    else:
        raise SyncError("missing RH secrets did not fail")
    from tempfile import TemporaryDirectory

    with TemporaryDirectory() as tmp:
        target = Path(tmp)
        (target / "kpi_summary.json").write_text('[{"sleeve":"crypto"}]\n', encoding="utf-8")
        (target / "meta.json").write_text(
            json.dumps({"source": "supabase", "fetched_at": "2026-09-28T01:08:27Z"}) + "\n",
            encoding="utf-8",
        )
        stamp_sync_failure(MISSING_RH, target)
        summary = (target / "kpi_summary.json").read_text(encoding="utf-8")
        meta = json.loads((target / "meta.json").read_text(encoding="utf-8"))
    if summary != '[{"sleeve":"crypto"}]\n':
        raise SyncError("stamp rewrote KPI JSON")
    if meta.get("export_status") != "error" or "RH_API_KEY" not in meta.get("export_error", ""):
        raise SyncError("stamp missed the RH secret error")
    if meta.get("fetched_at") != "2026-09-28T01:08:27Z":
        raise SyncError("stamp cleared fetched_at")
    oversell = [
        {
            "id": "88888888-8888-4888-8888-888888888888",
            "currency_code": "AAA",
            "side": "sell",
            "state": "filled",
            "cumulative_quantity": "100",
            "average_price": "5",
            "created_at": "2026-09-28T16:00:00Z",
        }
    ]
    try:
        rows_from_orders(oversell, existing)
    except SyncError as exc:
        if "exceeds open" not in str(exc):
            raise
    else:
        raise SyncError("oversell did not fail")
    if meta.get("warehouse_status"):
        raise SyncError("RH miss stamped a warehouse status")
    cursor = resolve_cursor(existing, Path("/no/such/rh-cursor.json"))
    if cursor != "2026-09-28T09:00:00Z":
        raise SyncError(f"cursor {cursor}")
    if query_start(cursor) != "2026-09-28T03:00:00Z":
        raise SyncError("overlap")
    print("self-test ok")
    return 0


def sell_id(row: dict) -> str:
    return order_id_from_why(row["why"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="Map fixture orders, check idempotency, and exit",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print fixture rows and do not call Robinhood or Supabase",
    )
    args = parser.parse_args(argv)
    try:
        if args.self_test:
            return self_test()
        if args.dry_run:
            print(json.dumps(fixture_rows(), indent=2))
            print("dry-run: fixture rows only; no insert", file=sys.stderr)
            return 0
        return sync()
    except SyncError as exc:
        print(str(exc), file=sys.stderr)
        if args.self_test or args.dry_run:
            return 1
        try:
            stamp_sync_failure(str(exc))
        except OSError as stamp_exc:
            print(f"Could not record RH sync status in data/meta.json: {stamp_exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
