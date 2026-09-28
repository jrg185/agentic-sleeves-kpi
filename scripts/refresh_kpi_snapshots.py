#!/usr/bin/env python3
"""Insert a fresh public.kpi_sleeve_snapshots row per sleeve.

public.kpi_summary is a view over the latest snapshot, and scripts/export_kpi.py
only SELECTs that view. Re-export cannot move as_of. This script reads fills from
public.kpi_trades (qty and price), marks what is still open, and INSERTs.

Seeds: crypto 300, equities 500, combined 800.
running_pnl = realized + unrealized
running_balance = start + running_pnl
realized = sum of pnl_trade_usd on closing legs
unrealized = open_qty * (mark - avg_cost)

Do not read public.kpi_trades_scrubbed. That view has no qty or price and joins
the latest snapshot, so it cannot refresh mark-to-market.

Credentials (same as export):
  SUPABASE_URL
  SUPABASE_SERVICE_ROLE_KEY
  SUPABASE_DB_URL            optional Postgres URL when REST cannot read or insert

Quote order, first success wins. An open ticker with no mark exits 1 and does
not INSERT.

crypto:
  1. Coinbase Exchange public ticker (no key). MNT uses product MANTLE-USD.
  2. Yahoo chart {SYMBOL}-USD regularMarketPrice (no key).
  3. CoinStats /coins when COINSTATS_API_KEY is set.
equities:
  1. Finnhub /quote when FINNHUB_API_KEY is set.
  2. Yahoo chart {SYMBOL} regularMarketPrice (no key).
  3. Alpha Vantage GLOBAL_QUOTE when ALPHA_VANTAGE_API_KEY is set.

Public Coinbase and Yahoo are enough for the current book. A missing optional
key is not an error when a public mark exists.

  python3 scripts/refresh_kpi_snapshots.py --self-test
  SUPABASE_URL=... SUPABASE_SERVICE_ROLE_KEY=... \\
    python3 scripts/refresh_kpi_snapshots.py --dry-run
  python3 scripts/refresh_kpi_snapshots.py

--dry-run prints the rows and does not INSERT. A live run that cannot insert,
including read-only transaction 25006 or a full disk, exits non-zero and leaves
the previous snapshot in place.

After a live run:

  select sleeve, as_of, realized_pnl_usd, unrealized_pnl_usd,
         running_pnl_usd, running_balance_usd, start_balance_usd
  from public.kpi_sleeve_snapshots
  order by as_of desc
  limit 6;
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal
from zoneinfo import ZoneInfo

DEFAULT_URL = "https://bsnqwgbshwszbjncglqx.supabase.co"
SLEEVES = ("crypto", "equities", "combined")
TRADE_SLEEVES = ("crypto", "equities")
START = {
    "crypto": Decimal("300"),
    "equities": Decimal("500"),
    "combined": Decimal("800"),
}
DUST = Decimal("0.00000001")
SYMBOL = re.compile(r"^[A-Z0-9]{1,15}$")
COINBASE_PRODUCT = {"MNT": "MANTLE-USD"}
WRITE_COLUMNS = (
    "sleeve",
    "as_of",
    "realized_pnl_usd",
    "unrealized_pnl_usd",
    "running_pnl_usd",
    "running_balance_usd",
    "start_balance_usd",
    "day_kill_pct",
    "day_target_pct",
    "notes",
)
USER_AGENT = "the-book-kpi-refresh/1"
FROZEN_PREFIX = "2026-09-27T23:48"
ET = ZoneInfo("America/New_York")


class RefreshError(RuntimeError):
    """A refresh that must not exit 0."""


class QuoteMiss(Exception):
    """One quote source did not return a usable mark."""


def q6(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.000001"))


def dec(value) -> Decimal | None:
    if value is None or value == "":
        return None
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def money(value: Decimal) -> str:
    quantized = value.quantize(Decimal("0.01"))
    sign = "-" if quantized < 0 else ""
    return f"{sign}${abs(quantized):.2f}"


def num_text(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return format(value, "f")


def parse_ts(value) -> dt.datetime:
    if isinstance(value, dt.datetime):
        parsed = value
    else:
        text = str(value).strip().replace("Z", "+00:00")
        if " " in text and "T" not in text:
            text = text.replace(" ", "T", 1)
        parsed = dt.datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ET)
    return parsed.astimezone(dt.timezone.utc)


def iso_z(moment: dt.datetime) -> str:
    return moment.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def redact(text: str, secrets: list[str]) -> str:
    clean = text
    for secret in secrets:
        if secret:
            clean = clean.replace(secret, "[redacted]")
    return clean[:400]


def loads(raw: bytes | str):
    text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
    return json.loads(text, parse_float=Decimal, parse_int=Decimal)


def http_json(url: str, headers: dict, timeout: int = 20, retries: int = 3):
    request = urllib.request.Request(url, headers=headers)
    last: Exception | None = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return loads(response.read())
        except json.JSONDecodeError:
            raise QuoteMiss("non-json") from None
        except urllib.error.HTTPError as exc:
            exc.read()
            last = exc
            if exc.code == 404:
                raise QuoteMiss(f"HTTP {exc.code}") from None
            if exc.code in {429, 500, 502, 503, 504} and attempt < retries - 1:
                time.sleep(1.1 * (attempt + 1))
                continue
            raise QuoteMiss(f"HTTP {exc.code}") from None
        except (urllib.error.URLError, TimeoutError) as exc:
            last = exc
            if attempt < retries - 1:
                time.sleep(1.1 * (attempt + 1))
                continue
            raise QuoteMiss("network error") from None
    raise QuoteMiss(f"request failed: {last}")


def positive_price(payload_price) -> Decimal:
    price = dec(payload_price)
    if price is None or price <= 0:
        raise QuoteMiss("non-positive price")
    return price


def coinbase_price(ticker: str) -> Decimal:
    product = COINBASE_PRODUCT.get(ticker, f"{ticker}-USD")
    url = "https://api.exchange.coinbase.com/products/" + urllib.parse.quote(product) + "/ticker"
    payload = http_json(url, {"User-Agent": USER_AGENT, "Accept": "application/json"})
    if not isinstance(payload, dict):
        raise QuoteMiss("coinbase payload")
    return positive_price(payload.get("price"))


def yahoo_price(symbol: str) -> Decimal:
    query = urllib.parse.urlencode({"interval": "1m", "range": "1d"})
    url = "https://query1.finance.yahoo.com/v8/finance/chart/" + urllib.parse.quote(symbol) + "?" + query
    payload = http_json(url, {"User-Agent": USER_AGENT, "Accept": "application/json"})
    chart = payload.get("chart") if isinstance(payload, dict) else None
    if not isinstance(chart, dict) or chart.get("error"):
        raise QuoteMiss("yahoo chart error")
    results = chart.get("result") or []
    if not results:
        raise QuoteMiss("yahoo empty chart")
    meta = results[0].get("meta") if isinstance(results[0], dict) else None
    if not isinstance(meta, dict):
        raise QuoteMiss("yahoo missing meta")
    return positive_price(meta.get("regularMarketPrice"))


def coinstats_price(ticker: str, key: str) -> Decimal:
    query = urllib.parse.urlencode({"currency": "USD", "symbol": ticker})
    url = "https://openapiv1.coinstats.app/coins?" + query
    payload = http_json(
        url,
        {"User-Agent": USER_AGENT, "Accept": "application/json", "X-API-KEY": key},
    )
    rows = []
    if isinstance(payload, dict):
        rows = payload.get("result") or payload.get("coins") or []
    elif isinstance(payload, list):
        rows = payload
    for row in rows:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("symbol") or "").upper()
        if symbol == ticker:
            return positive_price(row.get("price"))
    raise QuoteMiss("coinstats miss")


def finnhub_price(ticker: str, key: str) -> Decimal:
    query = urllib.parse.urlencode({"symbol": ticker, "token": key})
    url = "https://finnhub.io/api/v1/quote?" + query
    payload = http_json(url, {"User-Agent": USER_AGENT, "Accept": "application/json"})
    if not isinstance(payload, dict) or payload.get("error"):
        raise QuoteMiss("finnhub error")
    current = payload.get("c")
    if current in (None, 0, Decimal(0)):
        raise QuoteMiss("finnhub empty quote")
    return positive_price(current)


def alphavantage_price(ticker: str, key: str) -> Decimal:
    query = urllib.parse.urlencode(
        {"function": "GLOBAL_QUOTE", "symbol": ticker, "apikey": key}
    )
    url = "https://www.alphavantage.co/query?" + query
    payload = http_json(url, {"User-Agent": USER_AGENT, "Accept": "application/json"})
    if not isinstance(payload, dict):
        raise QuoteMiss("alphavantage payload")
    if payload.get("Note") or payload.get("Information"):
        raise QuoteMiss("alphavantage throttled")
    quote = payload.get("Global Quote") or {}
    if not isinstance(quote, dict):
        raise QuoteMiss("alphavantage quote")
    return positive_price(quote.get("05. price"))


def mark_for(sleeve: str, ticker: str, env: dict[str, str]) -> tuple[Decimal, str]:
    if not SYMBOL.fullmatch(ticker):
        raise RefreshError(f"open ticker {ticker!r} is not a mark symbol")
    attempts: list[tuple[str, callable]] = []
    if sleeve == "crypto":
        attempts.append(("coinbase:ticker", lambda: coinbase_price(ticker)))
        attempts.append(("yahoo:chart", lambda: yahoo_price(f"{ticker}-USD")))
        if env.get("COINSTATS_API_KEY"):
            attempts.append(("coinstats:coins", lambda: coinstats_price(ticker, env["COINSTATS_API_KEY"])))
    elif sleeve == "equities":
        if env.get("FINNHUB_API_KEY"):
            attempts.append(("finnhub:quote", lambda: finnhub_price(ticker, env["FINNHUB_API_KEY"])))
        attempts.append(("yahoo:chart", lambda: yahoo_price(ticker)))
        if env.get("ALPHA_VANTAGE_API_KEY"):
            attempts.append(
                ("alphavantage:global_quote", lambda: alphavantage_price(ticker, env["ALPHA_VANTAGE_API_KEY"]))
            )
    else:
        raise RefreshError(f"no quote path for sleeve {sleeve}")
    tried = []
    for source, fetch in attempts:
        try:
            price = fetch()
        except QuoteMiss as exc:
            tried.append(f"{source} ({exc})")
            continue
        return price, source
    detail = "; ".join(tried) if tried else "no quote source configured"
    raise RefreshError(f"{sleeve} {ticker}: no mark ({detail})")


def apply_books(fills: list[dict]) -> tuple[dict[str, Decimal], dict[tuple[str, str], dict]]:
    """Replay buys and sells. Realized is pnl_trade_usd on closes only."""
    realized = {sleeve: Decimal("0") for sleeve in TRADE_SLEEVES}
    book: dict[tuple[str, str], dict] = {}
    ordered = sorted(enumerate(fills), key=lambda item: (parse_ts(item[1]["timestamp_et"]), item[0]))
    for _, fill in ordered:
        sleeve = str(fill.get("sleeve") or "").strip().lower()
        if sleeve not in TRADE_SLEEVES:
            raise RefreshError(
                f"unexpected sleeve {sleeve!r} on kpi_trades; combined is the sum of the two desks"
            )
        ticker = str(fill.get("ticker") or "").strip().upper()
        if not ticker:
            raise RefreshError("kpi_trades row is missing ticker")
        side = str(fill.get("side") or "").strip().lower()
        if side not in {"buy", "sell"}:
            raise RefreshError(f"{sleeve} {ticker}: side {side!r} is not buy or sell")
        qty = dec(fill.get("qty"))
        if qty is None or qty <= 0:
            raise RefreshError(f"{sleeve} {ticker}: qty must be positive")
        price = dec(fill.get("avg_price"))
        pnl = dec(fill.get("pnl_trade_usd")) or Decimal("0")
        signed = qty if side == "buy" else -qty
        key = (sleeve, ticker)
        pos = book.get(key)
        open_qty = pos["qty"] if pos else Decimal("0")
        increasing = (signed > 0 and open_qty >= 0) or (signed < 0 and open_qty <= 0)
        if increasing:
            if price is None or price <= 0:
                raise RefreshError(f"{sleeve} {ticker}: opening fill is missing avg_price")
            new_qty = open_qty + signed
            if abs(open_qty) <= DUST:
                avg = price
            else:
                avg = (abs(open_qty) * pos["avg"] + abs(signed) * price) / abs(new_qty)
            book[key] = {"qty": new_qty, "avg": avg}
            continue
        close_qty = min(abs(open_qty), abs(signed))
        excess = abs(signed) - abs(open_qty)
        if excess > DUST:
            raise RefreshError(
                f"{sleeve} {ticker}: close qty {qty} exceeds open {abs(open_qty)}"
            )
        realized[sleeve] += pnl
        remain = abs(open_qty) - close_qty
        if remain <= DUST:
            book.pop(key, None)
        else:
            sign = Decimal("1") if open_qty > 0 else Decimal("-1")
            book[key] = {"qty": sign * remain, "avg": pos["avg"]}
    open_book = {key: pos for key, pos in book.items() if abs(pos["qty"]) > DUST}
    return realized, open_book


def note_for(sleeve: str, realized: Decimal, unrealized: Decimal) -> str:
    if sleeve == "combined":
        return (
            f"crypto+equities; realized {money(realized)}; unrealized {money(unrealized)}; "
            "book=start+realized+unrealized"
        )
    return (
        f"realized {money(realized)} closed exits; unrealized {money(unrealized)} open MTM; "
        "book=start+realized+unrealized"
    )


def prior_rail(prior: dict | None, key: str) -> Decimal | None:
    if not prior or key not in prior:
        return None
    return dec(prior.get(key))


def build_rows(
    fills: list[dict],
    marks: dict[tuple[str, str], Decimal],
    priors: dict[str, dict],
    as_of: str,
) -> tuple[list[dict], list[dict]]:
    realized_raw, book = apply_books(fills)
    missing = [f"{sleeve} {ticker}" for sleeve, ticker in sorted(book) if (sleeve, ticker) not in marks]
    if missing:
        raise RefreshError("open tickers have no mark: " + ", ".join(missing))
    unrealized_raw = {sleeve: Decimal("0") for sleeve in TRADE_SLEEVES}
    opens = []
    for (sleeve, ticker), pos in sorted(book.items()):
        mark = marks[(sleeve, ticker)]
        unreal = pos["qty"] * (mark - pos["avg"])
        unrealized_raw[sleeve] += unreal
        opens.append(
            {
                "sleeve": sleeve,
                "ticker": ticker,
                "qty": num_text(pos["qty"]),
                "avg_cost": num_text(pos["avg"]),
                "mark": num_text(mark),
                "unrealized_pnl_usd": num_text(q6(unreal)),
            }
        )
    realized = {sleeve: q6(realized_raw[sleeve]) for sleeve in TRADE_SLEEVES}
    unrealized = {sleeve: q6(unrealized_raw[sleeve]) for sleeve in TRADE_SLEEVES}
    realized["combined"] = realized["crypto"] + realized["equities"]
    unrealized["combined"] = unrealized["crypto"] + unrealized["equities"]
    rows = []
    for sleeve in SLEEVES:
        start = START[sleeve]
        running = realized[sleeve] + unrealized[sleeve]
        prior = priors.get(sleeve) or {}
        rows.append(
            {
                "sleeve": sleeve,
                "as_of": as_of,
                "realized_pnl_usd": num_text(realized[sleeve]),
                "unrealized_pnl_usd": num_text(unrealized[sleeve]),
                "running_pnl_usd": num_text(running),
                "running_balance_usd": num_text(start + running),
                "start_balance_usd": num_text(start),
                "day_kill_pct": num_text(prior_rail(prior, "day_kill_pct")),
                "day_target_pct": num_text(prior_rail(prior, "day_target_pct")),
                "notes": note_for(sleeve, realized[sleeve], unrealized[sleeve]),
            }
        )
    return rows, opens


def env_values() -> dict[str, str]:
    names = (
        "SUPABASE_URL",
        "SUPABASE_SERVICE_ROLE_KEY",
        "SUPABASE_DB_URL",
        "FINNHUB_API_KEY",
        "COINSTATS_API_KEY",
        "ALPHA_VANTAGE_API_KEY",
    )
    return {name: (os.environ.get(name) or "").strip() for name in names}


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
            return response.status, loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RefreshError(rest_error(method, path, exc.code, detail, [key])) from None
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise RefreshError("REST network error: " + redact(str(exc), [key])) from None


def rest_error(method: str, path: str, status: int, detail: str, secrets: list[str]) -> str:
    code = ""
    message = detail
    try:
        payload = json.loads(detail)
    except json.JSONDecodeError:
        payload = None
    if isinstance(payload, dict):
        code = str(payload.get("code") or "")
        message = str(payload.get("message") or detail)
    message = redact(message, secrets)
    table = path.split("?", 1)[0]
    if code == "25006" or "read-only transaction" in message.lower():
        return (
            "INSERT rejected: read-only transaction (25006). "
            "kpi_sleeve_snapshots was not updated."
        )
    if code in {"53100", "53200"} or "no space left" in message.lower():
        return f"INSERT rejected: database disk full ({code or status}). Snapshots were not updated."
    if "PGRST205" in message or status == 404:
        return f"REST {method} {table} is not exposed ({status})."
    return f"REST {method} {table} HTTP {status} {code}: {message}"


def fetch_trades_rest(base_url: str, key: str) -> list[dict]:
    rows: list[dict] = []
    page = 1000
    for offset in range(0, page * 20, page):
        path = (
            "/rest/v1/kpi_trades"
            "?select=sleeve,ticker,side,qty,avg_price,pnl_trade_usd,timestamp_et"
            "&order=timestamp_et.asc"
            f"&limit={page}&offset={offset}"
        )
        _status, payload = rest_call(base_url, key, path)
        if not isinstance(payload, list):
            raise RefreshError("REST kpi_trades did not return a row list")
        rows.extend(row for row in payload if isinstance(row, dict))
        if len(payload) < page:
            return rows
    raise RefreshError("REST kpi_trades exceeded 20000 rows; refusing a partial book")


def fetch_prior_rest(base_url: str, key: str, sleeve: str) -> dict | None:
    path = (
        "/rest/v1/kpi_sleeve_snapshots"
        f"?sleeve=eq.{sleeve}&select=*&order=as_of.desc&limit=1"
    )
    _status, payload = rest_call(base_url, key, path)
    if not isinstance(payload, list):
        raise RefreshError("REST kpi_sleeve_snapshots did not return a row list")
    return payload[0] if payload else None


def table_columns(base_url: str, key: str) -> set[str] | None:
    """Best-effort column check. A failed probe does not block the INSERT."""
    try:
        status_payload = rest_call(
            base_url,
            key,
            "/rest/v1/",
            extra_headers={"Accept": "application/openapi+json"},
        )
    except (RefreshError, ValueError, TypeError):
        return None
    spec = status_payload[1]
    if not isinstance(spec, dict):
        return None
    schemas = (spec.get("definitions") or {}) | ((spec.get("components") or {}).get("schemas") or {})
    props = (schemas.get("kpi_sleeve_snapshots") or {}).get("properties") or {}
    if not props:
        return None
    return set(props)


def prepare_payload(rows: list[dict], columns: set[str] | None) -> list[dict]:
    note_key = "notes"
    if columns is not None and "notes" not in columns and "note" in columns:
        note_key = "note"
    payload = []
    for row in rows:
        item = {}
        for name in WRITE_COLUMNS:
            key = note_key if name == "notes" else name
            item[key] = row[name]
        payload.append(item)
    return payload


def insert_rest(base_url: str, key: str, payload: list[dict]) -> list:
    _status, body = rest_call(
        base_url,
        key,
        "/rest/v1/kpi_sleeve_snapshots",
        method="POST",
        body=payload,
        extra_headers={"Prefer": "return=representation"},
    )
    if not isinstance(body, list) or len(body) != len(payload):
        raise RefreshError("INSERT did not return the new kpi_sleeve_snapshots rows")
    return body


def connect_db(db_url: str):
    try:
        import psycopg
    except ImportError as exc:
        raise RefreshError("psycopg is required for SUPABASE_DB_URL") from exc
    return psycopg.connect(db_url, connect_timeout=20)


def db_error(exc, secrets: list[str]) -> RefreshError:
    sqlstate = getattr(exc, "sqlstate", "") or ""
    message = redact(str(exc), secrets)
    if sqlstate == "25006" or "read-only transaction" in message.lower():
        return RefreshError(
            "INSERT rejected: read-only transaction (25006). kpi_sleeve_snapshots was not updated."
        )
    if sqlstate in {"53100", "53200"} or "no space left" in message.lower():
        return RefreshError(
            f"INSERT rejected: database disk full ({sqlstate}). Snapshots were not updated."
        )
    return RefreshError(f"database error {sqlstate}: {message}")


def fetch_trades_db(db_url: str, secrets: list[str]) -> list[dict]:
    sql = """
        select sleeve, ticker, side, qty, avg_price, pnl_trade_usd, timestamp_et
        from public.kpi_trades
        order by timestamp_et asc
    """
    try:
        with connect_db(db_url) as conn:
            with conn.cursor() as cur:
                cur.execute(sql)
                columns = [desc.name for desc in cur.description]
                return [dict(zip(columns, row)) for row in cur.fetchall()]
    except RefreshError:
        raise
    except Exception as exc:
        raise db_error(exc, secrets) from None


def fetch_prior_db(db_url: str, sleeve: str, secrets: list[str]) -> dict | None:
    sql = """
        select *
        from public.kpi_sleeve_snapshots
        where sleeve = %s
        order by as_of desc
        limit 1
    """
    try:
        with connect_db(db_url) as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (sleeve,))
                row = cur.fetchone()
                if row is None:
                    return None
                columns = [desc.name for desc in cur.description]
                return dict(zip(columns, row))
    except RefreshError:
        raise
    except Exception as exc:
        raise db_error(exc, secrets) from None


def insert_db(db_url: str, payload: list[dict], secrets: list[str]) -> list[dict]:
    columns = list(payload[0].keys())
    names = ", ".join(columns)
    places = ", ".join(["%s"] * len(columns))
    sql = (
        f"insert into public.kpi_sleeve_snapshots ({names}) "
        f"values ({places}) returning sleeve, as_of"
    )
    try:
        with connect_db(db_url) as conn:
            with conn.cursor() as cur:
                returned = []
                for row in payload:
                    cur.execute(sql, [row[name] for name in columns])
                    fetched = cur.fetchone()
                    if fetched:
                        returned.append({"sleeve": fetched[0], "as_of": fetched[1]})
            conn.commit()
    except RefreshError:
        raise
    except Exception as exc:
        raise db_error(exc, secrets) from None
    if len(returned) != len(payload):
        raise RefreshError("INSERT did not return the new kpi_sleeve_snapshots rows")
    return returned


def summary_as_of_rest(base_url: str, key: str) -> dict[str, dt.datetime]:
    _status, payload = rest_call(base_url, key, "/rest/v1/kpi_summary?select=sleeve,as_of")
    if not isinstance(payload, list):
        raise RefreshError("REST kpi_summary did not return a row list")
    found = {}
    for row in payload:
        if not isinstance(row, dict):
            continue
        sleeve = str(row.get("sleeve") or "").strip().lower()
        if sleeve in SLEEVES and row.get("as_of") is not None:
            moment = parse_ts(row["as_of"])
            if sleeve not in found or moment > found[sleeve]:
                found[sleeve] = moment
    return found


def summary_as_of_db(db_url: str, secrets: list[str]) -> dict[str, dt.datetime]:
    sql = "select sleeve, as_of from public.kpi_summary"
    try:
        with connect_db(db_url) as conn:
            with conn.cursor() as cur:
                cur.execute(sql)
                found = {}
                for sleeve, as_of in cur.fetchall():
                    key = str(sleeve or "").strip().lower()
                    if key in SLEEVES and as_of is not None:
                        moment = parse_ts(as_of)
                        if key not in found or moment > found[key]:
                            found[key] = moment
                return found
    except RefreshError:
        raise
    except Exception as exc:
        raise db_error(exc, secrets) from None


def assert_fresh(found: dict[str, dt.datetime], floor: dt.datetime) -> None:
    now = dt.datetime.now(dt.timezone.utc)
    missing = [sleeve for sleeve in SLEEVES if sleeve not in found]
    if missing:
        raise RefreshError(
            "public.kpi_summary is missing " + ", ".join(missing) + " after INSERT"
        )
    for sleeve, moment in found.items():
        text = iso_z(moment)
        if moment < floor or moment > now + dt.timedelta(minutes=2):
            frozen = " Still the frozen 2026-09-27 23:48Z snapshot." if text.startswith(FROZEN_PREFIX) else ""
            raise RefreshError(
                f"public.kpi_summary {sleeve} as_of {text} is not the row just inserted.{frozen}"
            )


def load_inputs(env: dict[str, str]) -> tuple[list[dict], dict[str, dict], str]:
    key = env.get("SUPABASE_SERVICE_ROLE_KEY") or ""
    db_url = env.get("SUPABASE_DB_URL") or ""
    base_url = env.get("SUPABASE_URL") or DEFAULT_URL
    secrets = [key, db_url]
    if key:
        try:
            trades = fetch_trades_rest(base_url, key)
            priors = {sleeve: fetch_prior_rest(base_url, key, sleeve) or {} for sleeve in SLEEVES}
            return trades, priors, "rest"
        except RefreshError as exc:
            if not db_url:
                if "kpi_trades" in str(exc):
                    raise RefreshError(
                        str(exc)
                        + " Refusing kpi_trades_scrubbed: that view has no qty or price."
                    ) from None
                raise
            print(f"REST read failed ({exc}); trying SUPABASE_DB_URL", file=sys.stderr)
    if not db_url:
        raise RefreshError(
            "SUPABASE_SERVICE_ROLE_KEY and SUPABASE_DB_URL are unset. "
            "Refusing to exit 0 with the frozen snapshot."
        )
    trades = fetch_trades_db(db_url, secrets)
    priors = {sleeve: fetch_prior_db(db_url, sleeve, secrets) or {} for sleeve in SLEEVES}
    return trades, priors, "db"


def resolve_marks(book_keys: list[tuple[str, str]], env: dict[str, str]) -> dict[tuple[str, str], tuple[Decimal, str]]:
    marks = {}
    errors = []
    for sleeve, ticker in book_keys:
        try:
            price, source = mark_for(sleeve, ticker, env)
        except RefreshError as exc:
            errors.append(str(exc))
            continue
        marks[(sleeve, ticker)] = (price, source)
        print(f"mark {sleeve} {ticker} {price} via {source}", file=sys.stderr)
    if errors:
        raise RefreshError("open tickers have no mark:\n" + "\n".join(errors))
    return marks


def refresh(dry_run: bool) -> int:
    env = env_values()
    secrets = [env.get("SUPABASE_SERVICE_ROLE_KEY", ""), env.get("SUPABASE_DB_URL", "")]
    trades, priors, source = load_inputs(env)
    if not trades:
        raise RefreshError("public.kpi_trades returned no rows; refusing to write a flat snapshot")
    _realized, book = apply_books(trades)
    quotes = resolve_marks(sorted(book), env)
    marks = {key: price for key, (price, _source) in quotes.items()}
    as_of = iso_z(dt.datetime.now(dt.timezone.utc))
    rows, opens = build_rows(trades, marks, priors, as_of)
    for item in opens:
        item["source"] = quotes[(item["sleeve"], item["ticker"])][1]
    plan = {
        "dry_run": dry_run,
        "source": source,
        "as_of": as_of,
        "prior_as_of": {
            sleeve: (iso_z(parse_ts(priors[sleeve]["as_of"])) if priors.get(sleeve) and priors[sleeve].get("as_of") else None)
            for sleeve in SLEEVES
        },
        "opens": opens,
        "rows": rows,
    }
    if dry_run:
        print(json.dumps(plan, indent=2))
        print("dry-run: no INSERT", file=sys.stderr)
        return 0
    key = env.get("SUPABASE_SERVICE_ROLE_KEY") or ""
    db_url = env.get("SUPABASE_DB_URL") or ""
    base_url = env.get("SUPABASE_URL") or DEFAULT_URL
    columns = table_columns(base_url, key) if key else None
    payload = prepare_payload(rows, columns)
    inserted = False
    rest_error_text = ""
    if key and source == "rest":
        try:
            insert_rest(base_url, key, payload)
            inserted = True
        except RefreshError as exc:
            rest_error_text = str(exc)
            if not db_url:
                raise
            print(f"REST insert failed ({exc}); trying SUPABASE_DB_URL", file=sys.stderr)
    if not inserted:
        if not db_url:
            raise RefreshError(rest_error_text or "no database credential for INSERT")
        insert_db(db_url, payload, secrets)
    floor = parse_ts(as_of) - dt.timedelta(seconds=2)
    if key:
        try:
            found = summary_as_of_rest(base_url, key)
        except RefreshError:
            if not db_url:
                raise
            found = summary_as_of_db(db_url, secrets)
    else:
        found = summary_as_of_db(db_url, secrets)
    assert_fresh(found, floor)
    for row in rows:
        print(
            "inserted {sleeve} as_of={as_of} realized={realized_pnl_usd} "
            "unrealized={unrealized_pnl_usd} running={running_pnl_usd} "
            "balance={running_balance_usd}".format(**row)
        )
    return 0


def self_test() -> int:
    fills = [
        {
            "sleeve": "crypto",
            "ticker": "aaa",
            "side": "buy",
            "qty": "10",
            "avg_price": "2",
            "pnl_trade_usd": "99",
            "timestamp_et": "2026-09-01T00:00:00Z",
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
    ]
    marks = {("crypto", "AAA"): Decimal("4"), ("equities", "QCOM"): Decimal("110")}
    priors = {"crypto": {"day_kill_pct": Decimal("-0.10"), "day_target_pct": Decimal("0.025")}}
    rows, opens = build_rows(fills, marks, priors, "2026-09-28T01:00:00Z")
    by = {row["sleeve"]: row for row in rows}
    # Opening buys ignore pnl_trade_usd. Avg cost (10*2 + 10*4) / 20 = 3. Sell 5 leaves 15.
    if by["crypto"]["realized_pnl_usd"] != "10.000000":
        raise RefreshError(f"crypto realized {by['crypto']['realized_pnl_usd']}")
    if by["crypto"]["unrealized_pnl_usd"] != "15.000000":
        raise RefreshError(f"crypto unrealized {by['crypto']['unrealized_pnl_usd']}")
    if by["crypto"]["running_pnl_usd"] != "25.000000":
        raise RefreshError("crypto running")
    if by["crypto"]["running_balance_usd"] != "325.000000":
        raise RefreshError("crypto balance")
    if by["crypto"]["day_kill_pct"] != "-0.10" or by["crypto"]["day_target_pct"] != "0.025":
        raise RefreshError("crypto rails were not copied")
    if by["equities"]["realized_pnl_usd"] != "0.000000":
        raise RefreshError("equities realized")
    if by["equities"]["unrealized_pnl_usd"] != "20.000000":
        raise RefreshError("equities unrealized")
    if by["equities"]["running_balance_usd"] != "520.000000":
        raise RefreshError("equities balance")
    if by["equities"]["day_kill_pct"] is not None:
        raise RefreshError("missing prior rail should stay null")
    if by["combined"]["realized_pnl_usd"] != "10.000000":
        raise RefreshError("combined realized")
    if by["combined"]["unrealized_pnl_usd"] != "35.000000":
        raise RefreshError("combined unrealized")
    if by["combined"]["running_balance_usd"] != "845.000000":
        raise RefreshError("combined balance")
    if by["combined"]["start_balance_usd"] != "800":
        raise RefreshError("combined seed")
    if len(opens) != 2:
        raise RefreshError("open count")
    try:
        build_rows(fills, {("crypto", "AAA"): Decimal("4")}, priors, "2026-09-28T01:00:00Z")
    except RefreshError as exc:
        if "QCOM" not in str(exc):
            raise
    else:
        raise RefreshError("missing mark did not fail")
    oversell = list(fills)
    oversell.append(
        {
            "sleeve": "equities",
            "ticker": "QCOM",
            "side": "sell",
            "qty": "9",
            "avg_price": "110",
            "pnl_trade_usd": "1",
            "timestamp_et": "2026-09-05T00:00:00Z",
        }
    )
    try:
        build_rows(oversell, marks, priors, "2026-09-28T01:00:00Z")
    except RefreshError as exc:
        if "exceeds open" not in str(exc):
            raise
    else:
        raise RefreshError("oversell did not fail")
    closed = [
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
    ]
    flat_rows, flat_opens = build_rows(closed, {}, {}, "2026-09-28T01:00:00Z")
    if flat_opens or flat_rows[0]["realized_pnl_usd"] != "3.000000":
        raise RefreshError("flat close should book realized and no mark")
    if flat_rows[0]["running_balance_usd"] != "303.000000":
        raise RefreshError("flat balance")
    short_fills = [
        {
            "sleeve": "equities",
            "ticker": "QCOM",
            "side": "sell",
            "qty": "2",
            "avg_price": "10",
            "pnl_trade_usd": "50",
            "timestamp_et": "2026-09-01T00:00:00Z",
        }
    ]
    short_rows, _short_opens = build_rows(
        short_fills,
        {("equities", "QCOM"): Decimal("9")},
        {},
        "2026-09-28T01:00:00Z",
    )
    by_short = {row["sleeve"]: row for row in short_rows}
    if by_short["equities"]["unrealized_pnl_usd"] != "2.000000":
        raise RefreshError(f"short unrealized {by_short['equities']['unrealized_pnl_usd']}")
    if by_short["equities"]["realized_pnl_usd"] != "0.000000":
        raise RefreshError("opening sell must not count pnl")
    message = rest_error(
        "POST",
        "/rest/v1/kpi_sleeve_snapshots",
        400,
        '{"code":"25006","message":"cannot execute INSERT in a read-only transaction"}',
        [],
    )
    if "25006" not in message or "not updated" not in message:
        raise RefreshError(message)
    print("self-test ok")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Read fills, mark the book, print rows, and do not INSERT",
    )
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="Check position math and the read-only INSERT error, then exit",
    )
    args = parser.parse_args(argv)
    try:
        if args.self_test:
            return self_test()
        return refresh(dry_run=args.dry_run)
    except RefreshError as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
