#!/usr/bin/env python3
"""Upsert filled Robinhood crypto orders into public.kpi_trades.

Robinhood has no fill webhook. Standing sync is an hourly poll, not a
15-minute Action and not a webhook. This script does not read ROBINHOOD_TOKEN.
That token is not a merge requirement.

Tonight Crypto Desk runs the poll. Once an hour, from a checkout of this repo:

  git pull
  python3 scripts/sync_rh_kpi_trades.py --print-cursor

Then Robinhood Trading MCP get_crypto_orders:

  rhs_account_number  546048042
  state               filled
  updated_at_gte      the timestamp --print-cursor printed

Paginate with the MCP cursor argument set to the previous response next
value until next is absent. Save every page's orders as one JSON document
and dispatch Export KPI. Actions upserts with the repo Supabase secrets,
refreshes marks, and exports. It also stores the next poll timestamp in
data/rh_kpi_sync_cursor.json when that file changes.

  gh workflow run export-kpi.yml --repo jrg185/the-book -f sync_rh_json="$(cat fills.json)"

A local upsert is the same mapper, if SUPABASE_URL and
SUPABASE_SERVICE_ROLE_KEY are in the environment:

  python3 scripts/sync_rh_kpi_trades.py --from-json fills.json

Optional later: Actions cron 0 * * * * calls --from-rh when RH_API_KEY and
RH_BASE64_PRIVATE_KEY are both set. If either is absent, that step exits 0
and does not call Robinhood. RH_AGENTIC_ACCOUNT overrides 546048042.

Bonus, not the standing path: repository_dispatch rh-fill, or a fill JSON
passed the moment a desk sees one. Same schema. The */15 cron only refreshes
marks.

If no fills feed is present, this script exits 0. Fail-loud (exit 1 and
stamp data/meta.json) only when a feed was explicitly requested and the input
or the upsert cannot be applied.

USDC and funding pairs are skipped. Sleeve is classified, not hardcoded:
explicit asset_class or instrument_type equity/stock/equities writes
sleeve "equities"; crypto writes "crypto". With no asset class, currency_code
or a BASE-QUOTE symbol is crypto, and a bare symbol such as QCOM is equities.
No orders are placed.

--from-json schema. PATH is a file, or - for stdin. SYNC_RH_JSON is the same
document, either raw JSON or a file path. RH_FILLS_PATH and data/rh_fills.json
are the same document. The JSON value is one of:

  { order }                          one fill, including client_payload
  [ order, ... ]                     --from-json and sync_rh_json only
  {"results": [ order, ... ]}
  {"data": {"results": [ order, ... ]}}
  {"data": [ order, ... ]}

Account numbers anywhere in the envelope are ignored and are not written.

Each order object:

  id                     uuid. Required on a filled order that is not skipped.
                         Stored as order_id.
  state                  "filled" is imported. Any other state is skipped.
                         A missing state is treated as filled.
  currency_code          MCP crypto ticker, such as "GRT". Preferred over symbol.
                         Implies sleeve crypto unless asset_class says equities.
  symbol                 "BASE-QUOTE", such as "BTC-USD", is crypto and BASE
                         is the ticker. A bare symbol such as "QCOM" is equities.
  currency_pair          Alias of symbol.
  asset_class            "crypto" or "cryptocurrency" -> sleeve crypto.
                         "equity", "stock", or "equities" -> sleeve equities.
                         Any other explicit class fails the sync.
  instrument_type        Same as asset_class. Explicit class wins over symbol.
  side                   "buy" or "sell".
  cumulative_quantity    Positive base quantity. First match wins.
  filled_asset_quantity  Same.
  quantity               Same.
  average_price          Positive.
  rounded_executed_notional   Optional notional. First match wins.
  total_executed_notional     Same.
  executed_notional           Same. If all three are absent, qty * price.
  fee                    Optional. First match wins.
  fee_charged            Same.
  fees                   Optional list of {"fee_data": {"fee_amount": "..."}}.
                         Summed only when fee and fee_charged are absent.
                         On conflict, a positive explicit fee fills fee_usd
                         when the stored fee is null or zero. why/notes rules
                         stay as they are. A missing fee is not written as a
                         replacement for a stored positive fee.
  executions             Optional list of {"timestamp": "..."}. The latest
                         timestamp is timestamp_et.
  created_at             Used when executions have no timestamp.
  updated_at             Used when created_at is also absent.
  why                    Optional human note. A machine stub is ignored.
  notes                  Optional human note.
  note                   Alias of notes.
  exit                   Optional human exit sentence. Folded into notes.
                         Not its own column. Objects are ignored.

Skipped without error: USDC, a USDC quote (BTC-USDC), ticker USD,
and any state other than filled.

Written columns: sleeve ("crypto" or "equities"), timestamp_et, ticker, side,
qty, avg_price, notional_usd, fee_usd, pnl_trade_usd, why, notes, order_id.
order_id is the Robinhood uuid. why and notes stay null unless the fill
carries a human why, notes, note, or exit. "RH Agentic backfill order <uuid>",
"RH Agentic sync order <uuid>", and the bare placeholder "backfill from RH"
are not human notes and are never written into why. Opening buys store
pnl_trade_usd 0. A closing sell stores price P&L versus the open average.
Insert is ON CONFLICT (order_id) DO UPDATE of why/notes only when the new
why is human and the stored why is null, blank, or a machine stub. A stored
human why is left unchanged. A later poll can attach ledger why, notes, or
exit to an existing stub by order_id. An older why of
"RH Agentic backfill order <uuid>" or "RH Agentic sync order <uuid>" still
counts as that uuid until the migration copies it onto order_id.

Supabase, same project as refresh and export:

  SUPABASE_URL
  SUPABASE_SERVICE_ROLE_KEY
  SUPABASE_DB_URL          When set, applies the migration, then upserts.

  python3 scripts/sync_rh_kpi_trades.py --self-test
  python3 scripts/sync_rh_kpi_trades.py --dry-run
  python3 scripts/sync_rh_kpi_trades.py --from-json fills.json
  python3 scripts/sync_rh_kpi_trades.py --dry-run --from-json fills.json
  python3 scripts/sync_rh_kpi_trades.py --from-json -

Example a desk can send the moment a GRT buy fills. Extra MCP fields are
ignored. This buy maps to sleeve crypto, ticker GRT, pnl_trade_usd 0:

  {
    "data": {
      "results": [
        {
          "id": "11111111-1111-4111-8111-111111111111",
          "currency_code": "GRT",
          "side": "buy",
          "state": "filled",
          "cumulative_quantity": "100",
          "average_price": "0.05",
          "rounded_executed_notional": "5",
          "fee": "0.01",
          "created_at": "2026-09-28T18:00:00Z"
        }
      ]
    }
  }

An equity fill in the same payload maps to sleeve equities. Equities Desk
sends these the same way when it detects a fill:

  {
    "id": "77777777-7777-4777-8777-777777777777",
    "asset_class": "equity",
    "symbol": "QCOM",
    "side": "buy",
    "state": "filled",
    "cumulative_quantity": "1",
    "average_price": "170",
    "created_at": "2026-09-28T15:00:00Z"
  }
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
FEEDS_PATH = DATA / "rh_fills.json"
MIGRATION_PATH = ROOT / "scripts" / "migrate_kpi_trades_order_id.sql"
DEFAULT_URL = "https://bsnqwgbshwszbjncglqx.supabase.co"
RH_BASE = "https://trading.robinhood.com"
DEFAULT_AGENTIC_ACCOUNT = "546048042"
BOOTSTRAP_CURSOR = "2026-09-26T00:00:00Z"
OVERLAP = dt.timedelta(hours=6)
DUST = Decimal("0.00000001")
PAGE_CAP = 50
SYMBOL = re.compile(r"^[A-Z0-9]{1,15}$")
UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
MACHINE_WHY = re.compile(
    rf"^RH Agentic (?:backfill|sync) order {UUID_RE.pattern}$",
    re.IGNORECASE,
)
PLACEHOLDER_NOTE = "backfill from RH"
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
    "notes",
    "order_id",
)
WHY_OPEN_SQL = """(
    kpi_trades.why is null
    or btrim(kpi_trades.why) = ''
    or kpi_trades.why ~* '^RH Agentic (backfill|sync) order [0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$'
)"""
# Positive incoming fee fills a null or zero stored fee. A zero payload does
# not clear a stored positive fee. why/notes still follow WHY_OPEN_SQL only.
FEE_REFRESH_SQL = """(
    excluded.fee_usd is not null
    and excluded.fee_usd > 0
    and (kpi_trades.fee_usd is null or kpi_trades.fee_usd = 0)
)"""
UPSERT_SQL = f"""
insert into public.kpi_trades (
    sleeve, timestamp_et, ticker, side, qty, avg_price,
    notional_usd, fee_usd, pnl_trade_usd, why, notes, order_id
) values (
    %(sleeve)s, %(timestamp_et)s, %(ticker)s, %(side)s, %(qty)s, %(avg_price)s,
    %(notional_usd)s, %(fee_usd)s, %(pnl_trade_usd)s, %(why)s, %(notes)s, %(order_id)s
)
on conflict (order_id) do update
set
    why = case
        when excluded.why is not null and {WHY_OPEN_SQL}
        then excluded.why
        else kpi_trades.why
    end,
    notes = case
        when excluded.why is not null and {WHY_OPEN_SQL}
        then coalesce(excluded.notes, kpi_trades.notes)
        else kpi_trades.notes
    end,
    fee_usd = case
        when {FEE_REFRESH_SQL}
        then excluded.fee_usd
        else kpi_trades.fee_usd
    end
where (excluded.why is not null and {WHY_OPEN_SQL})
   or {FEE_REFRESH_SQL}
"""
UPSERT_SQL_NO_NOTES = f"""
insert into public.kpi_trades (
    sleeve, timestamp_et, ticker, side, qty, avg_price,
    notional_usd, fee_usd, pnl_trade_usd, why, order_id
) values (
    %(sleeve)s, %(timestamp_et)s, %(ticker)s, %(side)s, %(qty)s, %(avg_price)s,
    %(notional_usd)s, %(fee_usd)s, %(pnl_trade_usd)s, %(why)s, %(order_id)s
)
on conflict (order_id) do update
set
    why = case
        when excluded.why is not null and {WHY_OPEN_SQL}
        then excluded.why
        else kpi_trades.why
    end,
    fee_usd = case
        when {FEE_REFRESH_SQL}
        then excluded.fee_usd
        else kpi_trades.fee_usd
    end
where (excluded.why is not null and {WHY_OPEN_SQL})
   or {FEE_REFRESH_SQL}
"""
FEE_BACKFILL_SQL = """
update public.kpi_trades
set fee_usd = %(fee_usd)s
where order_id = %(order_id)s
  and %(fee_usd)s::numeric > 0
  and (fee_usd is null or fee_usd = 0)
"""
NOTE_UPDATE_SQL = """
update public.kpi_trades
set why = %(why)s,
    notes = coalesce(%(notes)s, notes)
where order_id = %(order_id)s
  and why is not distinct from %(old_why)s
  and (
    why is null
    or btrim(why) = ''
    or why ~* '^RH Agentic (backfill|sync) order [0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$'
  )
"""
NOTE_UPDATE_SQL_NO_NOTES = """
update public.kpi_trades
set why = %(why)s
where order_id = %(order_id)s
  and why is not distinct from %(old_why)s
  and (
    why is null
    or btrim(why) = ''
    or why ~* '^RH Agentic (backfill|sync) order [0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$'
  )
"""

# Public example from https://docs.robinhood.com/crypto/trading/ (not a live key).
DOC_API_KEY = "rh-api-6148effc-c0b1-486c-8940-a1d099456be6"
DOC_PRIVATE_KEY = "xQnTJVeQLmw1/Mg2YimEViSpw/SdJcgNXZ5kQkAXNPU="
DOC_TIMESTAMP = "1698708981"
DOC_PATH = "/api/v1/crypto/trading/orders/"
DOC_SIGNATURE = "q/nEtxp/P2Or3hph3KejBqnw5o9qeuQ+hYRnB56FaHbjDsNUY9KhB1asMxohDnzdVFSD7StaTqjSd9U9HvaRAw=="

MIGRATION_HINT = (
    "public.kpi_trades.order_id is not ready. Apply scripts/migrate_kpi_trades_order_id.sql "
    "with SUPABASE_DB_URL before this sync. kpi_trades was not changed."
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


def human_note(value) -> str | None:
    """Human why or notes on a fill. Machine stubs and the bare placeholder are dropped."""
    if value is None:
        return None
    text = str(value).strip()
    if not text or text == PLACEHOLDER_NOTE or MACHINE_WHY.match(text):
        return None
    return text


def exit_note(value) -> str | None:
    """Human exit sentence. Nested objects and machine stubs are not notes."""
    if not isinstance(value, str):
        return None
    return human_note(value)


def notes_from_fill(order: dict) -> str | None:
    notes = None
    for key in ("notes", "note"):
        text = human_note(order.get(key))
        if text:
            notes = text
            break
    exit_text = exit_note(order.get("exit"))
    if not exit_text:
        return notes
    if not notes:
        return exit_text
    if exit_text == notes or exit_text in notes:
        return notes
    return f"{notes}\n{exit_text}"


def why_is_open(value) -> bool:
    """True when a stored why can accept a human note. Human text stays put."""
    if value is None:
        return True
    text = str(value).strip()
    if not text:
        return True
    return bool(MACHINE_WHY.match(text))


def note_patch(existing: dict | None, incoming: dict) -> dict | None:
    """why/notes to fill. None leaves the stored row unchanged.

    The incoming why must already be human. A machine stub or empty payload
    does not clear or replace a stored why. A stored human why is never updated.
    """
    if existing is not None and not why_is_open(existing.get("why")):
        return None
    why = incoming.get("why") or None
    if not why:
        return None
    patch = {"why": why}
    notes = incoming.get("notes") or None
    if notes:
        patch["notes"] = notes
    return patch


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


def sleeve_of(order: dict, order_id: str) -> str:
    """Warehouse sleeve. An explicit asset class wins over the symbol shape."""
    asset = str(order.get("asset_class") or order.get("instrument_type") or "").strip().lower()
    if asset in {"equity", "stock", "equities"}:
        return "equities"
    if asset in {"crypto", "cryptocurrency"}:
        return "crypto"
    if asset:
        raise SyncError(
            f"filled order {order_id} asset class {asset!r} is not crypto or equities"
        )
    if str(order.get("currency_code") or "").strip():
        return "crypto"
    symbol = str(order.get("symbol") or order.get("currency_pair") or "").strip()
    if symbol and "-" not in symbol:
        return "equities"
    if symbol:
        return "crypto"
    raise SyncError(f"filled order {order_id} has no asset class or symbol")


def is_funding(ticker: str, quote: str) -> bool:
    return ticker in {"USDC", "USD"} or quote == "USDC"


def fee_is_explicit(order: dict) -> bool:
    """True when the payload carried a fee. An absent fee is not a zero."""
    if dec(order.get("fee")) is not None:
        return True
    if dec(order.get("fee_charged")) is not None:
        return True
    fees = order.get("fees")
    if not isinstance(fees, list):
        return False
    for item in fees:
        if not isinstance(item, dict):
            continue
        data = item.get("fee_data")
        if isinstance(data, dict) and dec(data.get("fee_amount")) is not None:
            return True
    return False


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
    """Map one filled order. USDC, funding, and non-fills return None."""
    if not isinstance(order, dict):
        return None
    state = str(order.get("state") or order.get("derived_state") or "filled").strip().lower()
    if state != "filled":
        return None
    order_id = str(order.get("id") or "").strip().lower()
    if not UUID_RE.fullmatch(order_id):
        raise SyncError("filled order is missing an id")
    ticker, quote = split_symbol(order)
    if is_usdc(ticker, quote) or is_funding(ticker, quote):
        return None
    sleeve = sleeve_of(order, order_id)
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
    note = notes_from_fill(order)
    return {
        "sleeve": sleeve,
        "timestamp_et": fill_time(order),
        "ticker": ticker,
        "side": side,
        "qty": num_text(qty),
        "avg_price": num_text(price),
        "notional_usd": num_text(notional_of(order, qty, price)),
        "fee_usd": num_text(fee_of(order)),
        "fee_explicit": fee_is_explicit(order),
        "pnl_trade_usd": "0",
        "why": human_note(order.get("why")) or note,
        "notes": note,
        "order_id": order_id,
    }


def map_orders(orders: list[dict]) -> list[dict]:
    rows = []
    for order in orders:
        row = map_order(order)
        if row is not None:
            rows.append(row)
    return rows


def known_ids(existing: list[dict]) -> set[str]:
    """order_id column, plus uuids still sitting in an older machine why."""
    found: set[str] = set()
    for row in existing:
        order_id = str(row.get("order_id") or "").strip().lower()
        if order_id:
            found.add(order_id)
        found.update(uuids_in(str(row.get("why") or "")))
    return found


def drop_known(rows: list[dict], existing: list[dict]) -> list[dict]:
    """Keep the first row for each Robinhood order id. Stored ids win."""
    known = known_ids(existing)
    kept = []
    for row in rows:
        order_id = str(row.get("order_id") or "").strip().lower()
        if not order_id or order_id in known:
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
    fresh, _fills, _fees = plan_sync(orders, existing)
    return fresh


def fee_backfill_for(mapped: list[dict], existing: list[dict]) -> list[dict]:
    """Positive explicit fees for stored rows whose fee_usd is null or zero.

    Does not touch why or notes. A payload with no fee field is skipped.
    A stored positive fee is left in place.
    """
    index: dict[str, dict] = {}
    for row in existing:
        order_id = str(row.get("order_id") or "").strip().lower()
        if order_id and order_id not in index:
            index[order_id] = row
    patches = []
    seen: set[str] = set()
    for row in mapped:
        order_id = str(row.get("order_id") or "").strip().lower()
        if not order_id or order_id in seen or order_id not in index:
            continue
        seen.add(order_id)
        if not row.get("fee_explicit"):
            continue
        incoming = dec(row.get("fee_usd"))
        if incoming is None or incoming <= 0:
            continue
        stored = dec(index[order_id].get("fee_usd"))
        if stored is not None and stored != 0:
            continue
        patches.append({"order_id": order_id, "fee_usd": num_text(incoming)})
    return patches


def note_fills_for(mapped: list[dict], existing: list[dict]) -> list[dict]:
    """Human why/notes for rows already stored under order_id.

    Only an empty or machine stored why is filled. A human stored why is omitted
    so the caller leaves it unchanged.
    """
    index: dict[str, dict] = {}
    for row in existing:
        order_id = str(row.get("order_id") or "").strip().lower()
        if order_id and order_id not in index:
            index[order_id] = row
    fills = []
    seen: set[str] = set()
    for row in mapped:
        order_id = str(row.get("order_id") or "").strip().lower()
        if not order_id or order_id in seen or order_id not in index:
            continue
        seen.add(order_id)
        patch = note_patch(index[order_id], row)
        if not patch:
            continue
        fills.append(
            {
                "order_id": order_id,
                "old_why": index[order_id].get("why"),
                "why": patch["why"],
                "notes": patch.get("notes"),
            }
        )
    return fills


def plan_sync(orders: list[dict], existing: list[dict]) -> tuple[list[dict], list[dict], list[dict]]:
    """New priced fills, note fills, and fee backfills for stored order ids."""
    mapped = map_orders(orders)
    fresh = drop_known(mapped, existing)
    return assign_pnl(existing, fresh), note_fills_for(mapped, existing), fee_backfill_for(mapped, existing)


def orders_from_payload(payload) -> list[dict]:
    """Accept one order, a list, a results page, or a Robinhood MCP envelope."""
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        results = payload.get("results")
        if isinstance(results, list):
            return [item for item in results if isinstance(item, dict)]
        data = payload.get("data")
        if isinstance(data, dict) and isinstance(data.get("results"), list):
            return [item for item in data["results"] if isinstance(item, dict)]
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        if payload.get("id"):
            return [payload]
    raise SyncError("fills JSON must be one order, a list, or an object with results")


def load_fills(source: str) -> list[dict]:
    if source == "-":
        raw = sys.stdin.read()
    else:
        path = Path(source)
        if not path.is_file():
            raise SyncError(f"Fills file was not found: {source}. kpi_trades was not changed.")
        raw = path.read_text(encoding="utf-8")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SyncError("fills JSON could not be parsed. kpi_trades was not changed.") from exc
    return orders_from_payload(payload)


def sql_statements(text: str) -> list[str]:
    kept = []
    for line in text.splitlines():
        if line.strip().startswith("--"):
            continue
        kept.append(line)
    return [part.strip() for part in "\n".join(kept).split(";") if part.strip()]


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
                "Hourly Robinhood poll needs pynacl or cryptography to sign GET requests."
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
    if not re.fullmatch(r"[A-Za-z0-9]+", account):
        raise SyncError("RH_AGENTIC_ACCOUNT has unexpected characters")
    return (
        "/api/v2/crypto/trading/orders/"
        f"?account_number={account}&state=filled&updated_at_start={updated_at_start}&limit=100"
    )


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
    payload = {"updated_at": iso_z(updated_at)}
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def query_start(cursor: str) -> str:
    return iso_z(parse_ts(cursor) - OVERLAP)


def poll_start(path: Path = CURSOR_PATH) -> str:
    """updated_at_gte for the next MCP get_crypto_orders call."""
    return query_start(read_cursor(path) or BOOTSTRAP_CURSOR)


def advance_cursor(previous: str, orders: list[dict]) -> str | None:
    moments = [stamp for order in orders if (stamp := order_updated_at(order))]
    if not moments:
        return None
    latest = max(moments)
    if parse_ts(previous) > latest:
        return previous
    return iso_z(latest)


def remember_cursor(path: Path, orders: list[dict]) -> None:
    previous = read_cursor(path) or BOOTSTRAP_CURSOR
    nxt = advance_cursor(previous, orders)
    if nxt:
        write_cursor(path, nxt)


def agentic_account(env: dict[str, str]) -> str:
    chosen = (env.get("RH_AGENTIC_ACCOUNT") or DEFAULT_AGENTIC_ACCOUNT).strip()
    if not re.fullmatch(r"[A-Za-z0-9]+", chosen):
        raise SyncError("RH_AGENTIC_ACCOUNT has unexpected characters")
    return chosen


def rh_credentials(env: dict[str, str]) -> tuple[str, str, str] | None:
    api_key = (env.get("RH_API_KEY") or "").strip()
    private_key = (env.get("RH_BASE64_PRIVATE_KEY") or "").strip()
    if not api_key or not private_key:
        return None
    return api_key, private_key, agentic_account(env)


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
        detail = exc.read().decode("utf-8", errors="replace")
        lowered = detail.lower()
        if "notes" in lowered and ("pgrst204" in lowered or "42703" in lowered or "column" in lowered):
            raise SyncError("kpi_trades.notes is not a column") from None
        if "order_id" in lowered or "PGRST204" in detail:
            raise SyncError(MIGRATION_HINT) from None
        raise SyncError(f"REST {method} kpi_trades failed with HTTP {exc.code}. kpi_trades was not changed.") from None
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise SyncError("REST kpi_trades failed: network error. kpi_trades was not changed.") from exc


def fetch_trades_rest(base_url: str, key: str) -> list[dict]:
    rows: list[dict] = []
    page = 1000
    select = "sleeve,ticker,side,qty,avg_price,timestamp_et,why,pnl_trade_usd,order_id,fee_usd"
    for offset in range(0, page * 20, page):
        path = f"/rest/v1/kpi_trades?select={select}&order=timestamp_et.asc&limit={page}&offset={offset}"
        payload = rest_call(base_url, key, path)
        if not isinstance(payload, list):
            raise SyncError("REST kpi_trades did not return a row list")
        rows.extend(row for row in payload if isinstance(row, dict))
        if len(payload) < page:
            return rows
    raise SyncError("REST kpi_trades exceeded 20000 rows; refusing a partial sync")


def row_payload(rows: list[dict], include_notes: bool) -> list[dict]:
    columns = WRITE_COLUMNS if include_notes else tuple(column for column in WRITE_COLUMNS if column != "notes")
    return [{column: row[column] for column in columns} for row in rows]


def upsert_rest(base_url: str, key: str, rows: list[dict]) -> None:
    path = "/rest/v1/kpi_trades?on_conflict=order_id"
    headers = {"Prefer": "resolution=ignore-duplicates,return=minimal"}
    try:
        rest_call(base_url, key, path, method="POST", body=row_payload(rows, True), extra_headers=headers)
    except SyncError as exc:
        if "notes" not in str(exc).lower():
            raise
        print("kpi_trades.notes is absent; storing a human note on why only.", file=sys.stderr)
        rest_call(base_url, key, path, method="POST", body=row_payload(rows, False), extra_headers=headers)


def connect_db(db_url: str):
    try:
        import psycopg
    except ImportError as exc:
        raise SyncError("psycopg is required for SUPABASE_DB_URL") from exc
    return psycopg.connect(db_url, connect_timeout=20)


def fetch_trades_db(db_url: str) -> list[dict]:
    sql = """
        select sleeve, ticker, side, qty, avg_price, timestamp_et, why, pnl_trade_usd, order_id, fee_usd
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


def apply_migration(db_url: str) -> None:
    statements = sql_statements(MIGRATION_PATH.read_text(encoding="utf-8"))
    if not statements:
        raise SyncError("order_id migration file is empty")
    try:
        with connect_db(db_url) as conn:
            with conn.cursor() as cur:
                for statement in statements:
                    cur.execute(statement)
            conn.commit()
    except SyncError:
        raise
    except Exception as exc:
        raise SyncError(MIGRATION_HINT) from exc


def upsert_db(db_url: str, rows: list[dict], include_notes: bool = True) -> None:
    sql = UPSERT_SQL if include_notes else UPSERT_SQL_NO_NOTES
    try:
        with connect_db(db_url) as conn:
            with conn.cursor() as cur:
                cur.executemany(sql, row_payload(rows, include_notes))
            conn.commit()
    except SyncError:
        raise
    except Exception as exc:
        message = str(exc).lower()
        if include_notes and "notes" in message and "column" in message:
            print("kpi_trades.notes is absent; storing a human note on why only.", file=sys.stderr)
            upsert_db(db_url, rows, include_notes=False)
            return
        raise SyncError("database upsert into kpi_trades failed. The sync did not finish.") from exc


def note_fill_filter(order_id: str, old_why) -> str:
    if not UUID_RE.fullmatch(order_id):
        raise SyncError("note fill order_id is not a uuid")
    filt = "order_id=eq." + urllib.parse.quote(order_id, safe="")
    if old_why is None:
        return filt + "&why=is.null"
    text = str(old_why)
    if text.strip() == "":
        return filt + "&why=eq."
    return filt + "&why=eq." + urllib.parse.quote(text, safe="")


def apply_note_fills_rest(base_url: str, key: str, fills: list[dict], include_notes: bool = True) -> int:
    """PATCH why/notes only while the stored why is still the open value we read."""
    written = 0
    for fill in fills:
        body = {"why": fill["why"]}
        if include_notes and fill.get("notes"):
            body["notes"] = fill["notes"]
        path = "/rest/v1/kpi_trades?" + note_fill_filter(fill["order_id"], fill.get("old_why"))
        try:
            returned = rest_call(
                base_url,
                key,
                path,
                method="PATCH",
                body=body,
                extra_headers={"Prefer": "return=representation"},
            )
        except SyncError as exc:
            if include_notes and "notes" in str(exc).lower():
                print("kpi_trades.notes is absent; storing a human note on why only.", file=sys.stderr)
                return apply_note_fills_rest(base_url, key, fills, include_notes=False)
            raise
        rows = returned if isinstance(returned, list) else []
        if len(rows) > 1:
            raise SyncError(f"note fill matched {len(rows)} rows for {fill['order_id']}")
        written += len(rows)
    return written


def apply_note_fills_db(db_url: str, fills: list[dict], include_notes: bool = True) -> int:
    sql = NOTE_UPDATE_SQL if include_notes else NOTE_UPDATE_SQL_NO_NOTES
    written = 0
    try:
        with connect_db(db_url) as conn:
            with conn.cursor() as cur:
                for fill in fills:
                    cur.execute(
                        sql,
                        {
                            "order_id": fill["order_id"],
                            "old_why": fill.get("old_why"),
                            "why": fill["why"],
                            "notes": fill.get("notes"),
                        },
                    )
                    if cur.rowcount > 1:
                        raise SyncError(f"note fill matched {cur.rowcount} rows for {fill['order_id']}")
                    written += cur.rowcount
            conn.commit()
    except SyncError:
        raise
    except Exception as exc:
        message = str(exc).lower()
        if include_notes and "notes" in message and "column" in message:
            print("kpi_trades.notes is absent; storing a human note on why only.", file=sys.stderr)
            return apply_note_fills_db(db_url, fills, include_notes=False)
        raise SyncError("database note fill on kpi_trades failed. The sync did not finish.") from exc
    return written


def apply_note_fills(env: dict[str, str], fills: list[dict], source: str) -> int:
    if not fills:
        return 0
    key = env.get("SUPABASE_SERVICE_ROLE_KEY") or ""
    db_url = env.get("SUPABASE_DB_URL") or ""
    base_url = env.get("SUPABASE_URL") or DEFAULT_URL
    if key and source == "rest":
        try:
            return apply_note_fills_rest(base_url, key, fills)
        except SyncError:
            if not db_url:
                raise
            print("REST note fill failed; trying SUPABASE_DB_URL", file=sys.stderr)
    if not db_url:
        raise SyncError(MISSING_SB)
    return apply_note_fills_db(db_url, fills)


def apply_fee_backfill_rest(base_url: str, key: str, patches: list[dict]) -> int:
    """PATCH fee_usd only while the stored fee is still null or zero."""
    written = 0
    for patch in patches:
        order_id = str(patch.get("order_id") or "")
        if not UUID_RE.fullmatch(order_id):
            raise SyncError("fee backfill order_id is not a uuid")
        filt = "order_id=eq." + urllib.parse.quote(order_id, safe="")
        filt += "&or=(fee_usd.is.null,fee_usd.eq.0)"
        path = "/rest/v1/kpi_trades?" + filt
        returned = rest_call(
            base_url,
            key,
            path,
            method="PATCH",
            body={"fee_usd": patch["fee_usd"]},
            extra_headers={"Prefer": "return=representation"},
        )
        rows = returned if isinstance(returned, list) else []
        if len(rows) > 1:
            raise SyncError(f"fee backfill matched {len(rows)} rows")
        written += len(rows)
    return written


def apply_fee_backfill_db(db_url: str, patches: list[dict]) -> int:
    written = 0
    try:
        with connect_db(db_url) as conn:
            with conn.cursor() as cur:
                for patch in patches:
                    order_id = str(patch.get("order_id") or "")
                    if not UUID_RE.fullmatch(order_id):
                        raise SyncError("fee backfill order_id is not a uuid")
                    cur.execute(FEE_BACKFILL_SQL, {"order_id": order_id, "fee_usd": patch["fee_usd"]})
                    if cur.rowcount > 1:
                        raise SyncError(f"fee backfill matched {cur.rowcount} rows")
                    written += cur.rowcount
            conn.commit()
    except SyncError:
        raise
    except Exception as exc:
        raise SyncError("database fee backfill on kpi_trades failed. The sync did not finish.") from exc
    return written


def apply_fee_backfill(env: dict[str, str], patches: list[dict], source: str) -> int:
    if not patches:
        return 0
    key = env.get("SUPABASE_SERVICE_ROLE_KEY") or ""
    db_url = env.get("SUPABASE_DB_URL") or ""
    base_url = env.get("SUPABASE_URL") or DEFAULT_URL
    if key and source == "rest":
        try:
            return apply_fee_backfill_rest(base_url, key, patches)
        except SyncError:
            if not db_url:
                raise
            print("REST fee backfill failed; trying SUPABASE_DB_URL", file=sys.stderr)
    if not db_url:
        raise SyncError(MISSING_SB)
    return apply_fee_backfill_db(db_url, patches)


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


def upsert_rows(env: dict[str, str], rows: list[dict], source: str) -> None:
    if not rows:
        return
    key = env.get("SUPABASE_SERVICE_ROLE_KEY") or ""
    db_url = env.get("SUPABASE_DB_URL") or ""
    base_url = env.get("SUPABASE_URL") or DEFAULT_URL
    if key and source == "rest":
        try:
            upsert_rest(base_url, key, rows)
            return
        except SyncError:
            if not db_url:
                raise
            print("REST upsert failed; trying SUPABASE_DB_URL", file=sys.stderr)
    if not db_url:
        raise SyncError(MISSING_SB)
    upsert_db(db_url, rows)


def env_values() -> dict[str, str]:
    names = (
        "SUPABASE_URL",
        "SUPABASE_SERVICE_ROLE_KEY",
        "SUPABASE_DB_URL",
        "SYNC_RH_JSON",
        "RH_FILLS_PATH",
        "RH_API_KEY",
        "RH_BASE64_PRIVATE_KEY",
        "RH_AGENTIC_ACCOUNT",
    )
    return {name: (os.environ.get(name) or "").strip() for name in names}


def feed_requested(env: dict[str, str], from_json: str | None, default_feed: Path) -> bool:
    """True when the operator asked for an upsert. A token alone is not a request."""
    if (from_json or "").strip():
        return True
    if (env.get("SYNC_RH_JSON") or "").strip():
        return True
    if (env.get("RH_FILLS_PATH") or "").strip():
        return True
    return default_feed.is_file()


def load_sync_rh_json(value: str) -> list[dict]:
    text = value.strip()
    if text == "-":
        return load_fills("-")
    if text[:1] in "{[":
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise SyncError("fills JSON could not be parsed. kpi_trades was not changed.") from exc
        return orders_from_payload(payload)
    return load_fills(text)


def load_requested_orders(env: dict[str, str], from_json: str | None, default_feed: Path) -> list[dict]:
    """Load the feed the operator named. Missing or bad input raises."""
    if (from_json or "").strip():
        return load_fills(from_json)
    inline = (env.get("SYNC_RH_JSON") or "").strip()
    if inline:
        return load_sync_rh_json(inline)
    feed = (env.get("RH_FILLS_PATH") or "").strip()
    if feed:
        return load_fills(feed)
    if default_feed.is_file():
        return load_fills(str(default_feed))
    raise SyncError("RH fill sync was requested without a fills feed. kpi_trades was not changed.")


def public_sync_message(message: str) -> str:
    text = message
    for name in (
        "SUPABASE_SERVICE_ROLE_KEY",
        "SUPABASE_DB_URL",
        "SYNC_RH_JSON",
        "RH_API_KEY",
        "RH_BASE64_PRIVATE_KEY",
        "ROBINHOOD_TOKEN",
    ):
        secret = (os.environ.get(name) or "").strip()
        if len(secret) >= 6:
            text = text.replace(secret, "[redacted]")
    lowered = text.lower()
    if "read-only" in lowered or "readonly" in lowered or "disk full" in lowered or "25006" in text:
        text = "RH fill sync failed. kpi_trades was not changed."
    return text[:500]


def stamp_sync_failure(message: str, target: Path | None = None) -> None:
    """Record a requested sync failure on meta.json. Does not rewrite KPI numbers."""
    sys.path.insert(0, str(ROOT / "scripts"))
    import export_kpi

    export_kpi.stamp_export_failure(target or DATA, "error", public_sync_message(message))


def sync(
    from_json: str | None = None,
    default_feed: Path = FEEDS_PATH,
    env: dict[str, str] | None = None,
    from_rh: bool = False,
    cursor_path: Path = CURSOR_PATH,
) -> int:
    env = env_values() if env is None else env
    orders: list[dict] | None = None
    creds = None
    if feed_requested(env, from_json, default_feed):
        orders = load_requested_orders(env, from_json, default_feed)
    elif from_rh:
        creds = rh_credentials(env)
        if not creds:
            raise SyncError(
                "Hourly Robinhood poll was requested but RH_API_KEY and "
                "RH_BASE64_PRIVATE_KEY are unset. No orders were read. "
                "ROBINHOOD_TOKEN is not used. kpi_trades was not changed."
            )
    else:
        print("rh sync skipped: no fill payload and no Robinhood API secrets.")
        return 0
    if not (env.get("SUPABASE_SERVICE_ROLE_KEY") or env.get("SUPABASE_DB_URL")):
        raise SyncError(MISSING_SB)
    if env.get("SUPABASE_DB_URL"):
        apply_migration(env["SUPABASE_DB_URL"])
    existing, source = load_trades(env)
    if orders is None:
        if creds is None:
            raise SyncError("Hourly Robinhood poll has no API credentials. kpi_trades was not changed.")
        api_key, private_key, account = creds
        start = query_start(read_cursor(cursor_path) or BOOTSTRAP_CURSOR)
        orders = fetch_filled_orders(api_key, private_key, account, start)
    fresh, fills, fee_patches = plan_sync(orders, existing)
    upsert_rows(env, fresh, source)
    noted = apply_note_fills(env, fills, source)
    fees_written = apply_fee_backfill(env, fee_patches, source)
    remember_cursor(cursor_path, orders)
    print(
        f"rh sync warehouse={source} fetched={len(orders)} upserted={len(fresh)} "
        f"noted={noted} fee_backfill={fees_written}"
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
    by_id = {row["order_id"]: row for row in rows}
    if "11111111-1111-4111-8111-111111111111" in by_id:
        raise SyncError("backfill order id was inserted again")
    if "33333333-3333-4333-8333-333333333333" in by_id or "44444444-4444-4444-8444-444444444444" in by_id:
        raise SyncError("USDC order was not skipped")
    if "66666666-6666-4666-8666-666666666666" in by_id:
        raise SyncError("canceled order was inserted")
    if any(row.get("why") is not None or row.get("notes") is not None for row in rows):
        raise SyncError("mapped fill invented a why or notes")
    if "sync order" in json.dumps(rows) or "backfill order" in json.dumps(rows):
        raise SyncError("mapped fill invented a sync-order why")
    sell = by_id["22222222-2222-4222-8222-222222222222"]
    if sell.get("why") is not None or sell.get("notes") is not None:
        raise SyncError(f"why {sell['why']!r} notes {sell.get('notes')!r}")
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
    if sell["order_id"] != "22222222-2222-4222-8222-222222222222":
        raise SyncError("sell order_id")
    if sell["sleeve"] != "crypto":
        raise SyncError("sell sleeve")
    qcom = by_id["77777777-7777-4777-8777-777777777777"]
    if qcom["sleeve"] != "equities" or qcom["ticker"] != "QCOM" or qcom["pnl_trade_usd"] != "0":
        raise SyncError(f"equity sleeve {qcom}")
    bare_equity = map_order(
        {
            "id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            "symbol": "AAPL",
            "side": "buy",
            "state": "filled",
            "cumulative_quantity": "1",
            "average_price": "170",
            "created_at": "2026-09-28T15:30:00Z",
        }
    )
    if not bare_equity or bare_equity["sleeve"] != "equities" or bare_equity["ticker"] != "AAPL":
        raise SyncError("bare equity symbol was not classified")
    if bare_equity.get("why") is not None or bare_equity.get("notes") is not None:
        raise SyncError("bare equity invented a why")
    try:
        sleeve_of(
            {"asset_class": "option", "symbol": "QCOM"},
            "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
        )
    except SyncError as exc:
        if "not crypto or equities" not in str(exc):
            raise
    else:
        raise SyncError("unknown asset class was accepted")
    again = drop_known(rows, [{"order_id": sell["order_id"]}, {"order_id": btc["order_id"]}])
    if any(row["order_id"] in {sell["order_id"], btc["order_id"]} for row in again):
        raise SyncError("second pass inserted a known order id")
    legacy = drop_known(rows, [{"why": f"RH Agentic sync order {sell['order_id']}"}])
    if any(row["order_id"] == sell["order_id"] for row in legacy):
        raise SyncError("legacy why uuid was not treated as known")
    bare = map_order(
        {
            "id": "99999999-9999-4999-8999-999999999999",
            "currency_code": "OP",
            "side": "buy",
            "cumulative_quantity": "2",
            "average_price": "1.5",
            "created_at": "2026-09-28T13:00:00Z",
        }
    )
    if not bare or bare["ticker"] != "OP" or bare["order_id"] != "99999999-9999-4999-8999-999999999999":
        raise SyncError("order JSON without state was dropped")
    if bare.get("why") is not None or bare.get("notes") is not None:
        raise SyncError("order without a note invented a why")
    stub = map_order(
        {
            "id": "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
            "currency_code": "OP",
            "side": "buy",
            "state": "filled",
            "cumulative_quantity": "1",
            "average_price": "1.25",
            "created_at": "2026-09-28T14:00:00Z",
            "why": "RH Agentic sync order cccccccc-cccc-4ccc-8ccc-cccccccccccc",
            "notes": "backfill from RH",
        }
    )
    if not stub or stub["why"] is not None or stub["notes"] is not None:
        raise SyncError(f"machine why and placeholder were stored {stub}")
    if stub["order_id"] != "cccccccc-cccc-4ccc-8ccc-cccccccccccc":
        raise SyncError("rejected note dropped order_id")
    longer = map_order(
        {
            "id": "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
            "currency_code": "OP",
            "side": "buy",
            "state": "filled",
            "cumulative_quantity": "1",
            "average_price": "1.25",
            "created_at": "2026-09-28T14:05:00Z",
            "why": "backfill from RH yesterday",
        }
    )
    if not longer or longer["why"] != "backfill from RH yesterday" or longer["notes"] is not None:
        raise SyncError(f"a longer backfill sentence was rejected {longer}")
    thesis = map_order(
        {
            "id": "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
            "symbol": "SOL-USD",
            "side": "buy",
            "state": "filled",
            "cumulative_quantity": "1",
            "average_price": "2",
            "created_at": "2026-09-28T14:10:00Z",
            "why": "backfill from RH",
            "notes": "RH Agentic backfill order eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
            "note": "breakout add",
        }
    )
    if not thesis or thesis["why"] != "breakout add" or thesis["notes"] != "breakout add":
        raise SyncError(f"note field was not kept {thesis}")
    both = map_order(
        {
            "id": "ffffffff-ffff-4fff-8fff-ffffffffffff",
            "symbol": "SOL-USD",
            "side": "sell",
            "state": "filled",
            "cumulative_quantity": "1",
            "average_price": "3",
            "created_at": "2026-09-28T14:20:00Z",
            "why": "take the target",
            "notes": "soft target hit",
        }
    )
    if not both or both["why"] != "take the target" or both["notes"] != "soft target hit":
        raise SyncError(f"why and notes were merged {both}")
    kept = rows_from_orders(
        [
            {
                "id": sell["order_id"],
                "currency_code": "AAA",
                "side": "sell",
                "state": "filled",
                "cumulative_quantity": "4",
                "average_price": "5",
                "created_at": "2026-09-28T11:00:00Z",
                "why": "replacement thesis",
            }
        ],
        [
            {
                "order_id": sell["order_id"],
                "why": "keep this thesis",
                "sleeve": "crypto",
                "ticker": "AAA",
                "side": "sell",
                "qty": "4",
                "avg_price": "5",
                "timestamp_et": "2026-09-28T11:00:00+00:00",
                "pnl_trade_usd": "12",
            }
        ],
    )
    if kept:
        raise SyncError("repeat order_id was planned over an existing human why")
    replacement = map_orders(
        [
            {
                "id": sell["order_id"],
                "currency_code": "AAA",
                "side": "sell",
                "state": "filled",
                "cumulative_quantity": "4",
                "average_price": "5",
                "created_at": "2026-09-28T11:00:00Z",
                "why": "replacement thesis",
            }
        ]
    )
    human_existing = {
        "order_id": sell["order_id"],
        "why": "keep this thesis",
    }
    if note_fills_for(replacement, [human_existing]):
        raise SyncError("conflict with human already present was changed")
    if note_patch(human_existing, replacement[0]) is not None:
        raise SyncError("conflict with human already present was changed")
    stub_existing = {
        "order_id": sell["order_id"],
        "why": f"RH Agentic sync order {sell['order_id']}",
    }
    stub_fill = note_fills_for(replacement, [stub_existing])
    if stub_fill != [
        {
            "order_id": sell["order_id"],
            "old_why": stub_existing["why"],
            "why": "replacement thesis",
            "notes": None,
        }
    ]:
        raise SyncError(f"machine stub was not filled {stub_fill}")
    empty_fill = note_fills_for(replacement, [{"order_id": sell["order_id"], "why": None}])
    if len(empty_fill) != 1 or empty_fill[0]["why"] != "replacement thesis":
        raise SyncError(f"empty why was not filled {empty_fill}")
    if note_fills_for(map_orders(
        [
            {
                "id": sell["order_id"],
                "currency_code": "AAA",
                "side": "sell",
                "state": "filled",
                "cumulative_quantity": "4",
                "average_price": "5",
                "created_at": "2026-09-28T11:00:00Z",
            }
        ]
    ), [human_existing]):
        raise SyncError("empty payload would change a human why")
    if note_patch(stub_existing, {"why": None, "notes": None}) is not None:
        raise SyncError("empty payload would clear a machine stub")
    priced = map_orders(
        [
            {
                "id": sell["order_id"],
                "currency_code": "AAA",
                "side": "sell",
                "state": "filled",
                "cumulative_quantity": "4",
                "average_price": "5",
                "fee": "0.20",
                "created_at": "2026-09-28T11:00:00Z",
                "why": "replacement thesis",
            }
        ]
    )
    if not priced or priced[0].get("fee_explicit") is not True:
        raise SyncError("explicit fee was not marked")
    zero_stored = {"order_id": sell["order_id"], "why": "keep this thesis", "fee_usd": "0"}
    fee_patch = fee_backfill_for(priced, [zero_stored])
    if fee_patch != [{"order_id": sell["order_id"], "fee_usd": "0.2"}]:
        raise SyncError(f"null/zero fee was not backfilled {fee_patch}")
    if fee_backfill_for(priced, [{"order_id": sell["order_id"], "why": "keep this thesis", "fee_usd": None}]) != fee_patch:
        raise SyncError("null stored fee was not backfilled")
    if fee_backfill_for(priced, [{"order_id": sell["order_id"], "why": "keep this thesis", "fee_usd": "1.25"}]):
        raise SyncError("positive stored fee was overwritten")
    if fee_backfill_for(replacement, [zero_stored]):
        raise SyncError("a payload with no fee field was treated as a fee")
    explicit_zero = map_orders(
        [
            {
                "id": sell["order_id"],
                "currency_code": "AAA",
                "side": "sell",
                "state": "filled",
                "cumulative_quantity": "4",
                "average_price": "5",
                "fee": "0",
                "created_at": "2026-09-28T11:00:00Z",
            }
        ]
    )
    if not explicit_zero or explicit_zero[0].get("fee_explicit") is not True or explicit_zero[0]["fee_usd"] != "0":
        raise SyncError(f"explicit zero fee was invented or dropped {explicit_zero}")
    if fee_backfill_for(explicit_zero, [zero_stored]):
        raise SyncError("explicit zero replaced a stored fee")
    if note_fills_for(priced, [zero_stored]):
        raise SyncError("fee backfill changed a human why")
    stored_book = {
        "order_id": sell["order_id"],
        "why": "keep this thesis",
        "fee_usd": "0",
        "sleeve": "crypto",
        "ticker": "AAA",
        "side": "sell",
        "qty": "4",
        "avg_price": "5",
        "timestamp_et": "2026-09-28T11:00:00+00:00",
        "pnl_trade_usd": "12",
    }
    fresh_plan, note_plan, fee_plan = plan_sync(
        [
            {
                "id": sell["order_id"],
                "currency_code": "AAA",
                "side": "sell",
                "state": "filled",
                "cumulative_quantity": "4",
                "average_price": "5",
                "fee": "0.20",
                "created_at": "2026-09-28T11:00:00Z",
            }
        ],
        [stored_book],
    )
    if fresh_plan or note_plan or fee_plan != [{"order_id": sell["order_id"], "fee_usd": "0.2"}]:
        raise SyncError(f"known order was reinserted instead of a fee backfill {fresh_plan} {note_plan} {fee_plan}")
    ledger_id = "12121212-1212-4121-8121-121212121212"
    with_why = map_order(
        {
            "id": ledger_id,
            "currency_code": "OP",
            "side": "buy",
            "state": "filled",
            "cumulative_quantity": "1",
            "average_price": "1",
            "created_at": "2026-09-28T14:30:00Z",
            "why": "ledger why",
            "exit": "target hit",
        }
    )
    if not with_why or with_why["why"] != "ledger why" or with_why["notes"] != "target hit":
        raise SyncError(f"human why was not stored {with_why}")
    if with_why["order_id"] != ledger_id or "exit" in with_why:
        raise SyncError("human why did not keep order_id, or exit became a column")
    without = map_order(
        {
            "id": ledger_id,
            "currency_code": "OP",
            "side": "buy",
            "state": "filled",
            "cumulative_quantity": "1",
            "average_price": "1",
            "created_at": "2026-09-28T14:30:00Z",
        }
    )
    if not without or without["why"] is not None or without["notes"] is not None:
        raise SyncError(f"missing note was stored {without}")
    if without["order_id"] != ledger_id:
        raise SyncError("payload without a note dropped order_id")
    exit_only = map_order(
        {
            "id": ledger_id,
            "currency_code": "OP",
            "side": "buy",
            "state": "filled",
            "cumulative_quantity": "1",
            "average_price": "1",
            "created_at": "2026-09-28T14:30:00Z",
            "exit": "target hit",
            "notes": "scale out",
        }
    )
    if not exit_only or exit_only["why"] != "scale out\ntarget hit" or exit_only["notes"] != "scale out\ntarget hit":
        raise SyncError(f"exit was not folded into notes {exit_only}")
    messy_exit = map_order(
        {
            "id": ledger_id,
            "currency_code": "OP",
            "side": "buy",
            "state": "filled",
            "cumulative_quantity": "1",
            "average_price": "1",
            "created_at": "2026-09-28T14:30:00Z",
            "exit": {"reason": "target hit"},
        }
    )
    if not messy_exit or messy_exit["why"] is not None or messy_exit["notes"] is not None:
        raise SyncError(f"exit object was stored {messy_exit}")
    stored = rows_from_orders(
        [
            {
                "id": ledger_id,
                "currency_code": "OP",
                "side": "buy",
                "state": "filled",
                "cumulative_quantity": "1",
                "average_price": "1",
                "created_at": "2026-09-28T14:30:00Z",
                "why": "ledger why",
            }
        ],
        [],
    )
    if len(stored) != 1 or stored[0]["why"] != "ledger why" or stored[0]["order_id"] != ledger_id:
        raise SyncError(f"human why was not in the insert plan {stored}")
    bare_plan = rows_from_orders(
        [
            {
                "id": ledger_id,
                "currency_code": "OP",
                "side": "buy",
                "state": "filled",
                "cumulative_quantity": "1",
                "average_price": "1",
                "created_at": "2026-09-28T14:30:00Z",
            }
        ],
        [],
    )
    if len(bare_plan) != 1 or bare_plan[0]["why"] is not None or bare_plan[0]["order_id"] != ledger_id:
        raise SyncError(f"insert plan invented a why {bare_plan}")
    envelope = orders_from_payload(
        {
            "data": {
                "rhs_account_number": "SHOULD_NOT_LEAK",
                "results": fixture_orders(),
            }
        }
    )
    if len(envelope) != len(fixture_orders()):
        raise SyncError("MCP envelope was not unwrapped")
    one = orders_from_payload(
        {
            "id": "11111111-1111-4111-8111-111111111111",
            "currency_code": "GRT",
            "side": "buy",
            "state": "filled",
            "cumulative_quantity": "100",
            "average_price": "0.05",
            "created_at": "2026-09-28T18:00:00Z",
        }
    )
    if len(one) != 1 or one[0].get("currency_code") != "GRT":
        raise SyncError("single fill object was not accepted")
    mapped_one = rows_from_orders(one, [])
    if len(mapped_one) != 1 or mapped_one[0]["sleeve"] != "crypto" or mapped_one[0]["ticker"] != "GRT":
        raise SyncError("single fill did not map to crypto GRT")
    leaked = json.dumps(rows_from_orders(envelope, existing))
    if "SHOULD_NOT_LEAK" in leaked:
        raise SyncError("account number leaked into a kpi_trades row")
    if "on conflict (order_id) do update" not in UPSERT_SQL.lower():
        raise SyncError("upsert does not fill an open why on conflict")
    if "excluded.why is not null" not in UPSERT_SQL.lower():
        raise SyncError("conflict update can write an empty why")
    update_set = UPSERT_SQL.lower().split("do update", 1)[1].split("where", 1)[0]
    if "fee_usd" not in update_set:
        raise SyncError("conflict update does not refresh fee_usd")
    if "qty" in update_set or "pnl_trade_usd" in update_set or "avg_price" in update_set or "notional_usd" in update_set:
        raise SyncError("conflict update writes price or quantity")
    if "fee_usd" not in UPSERT_SQL_NO_NOTES.lower().split("do update", 1)[1]:
        raise SyncError("notes-less conflict update does not refresh fee_usd")
    if "fee_usd is null or fee_usd = 0" not in FEE_BACKFILL_SQL.lower():
        raise SyncError("fee backfill can overwrite a stored positive fee")
    if "rh agentic (backfill|sync) order" not in UPSERT_SQL.lower():
        raise SyncError("conflict update does not recognize a machine why")
    if "notes" not in UPSERT_SQL or "do update" not in UPSERT_SQL_NO_NOTES.lower():
        raise SyncError("notes upsert is not optional and conflict-safe")
    if "excluded.why is not null" not in UPSERT_SQL_NO_NOTES.lower():
        raise SyncError("notes-less conflict update can write an empty why")
    if "is not distinct from" not in NOTE_UPDATE_SQL.lower():
        raise SyncError("note fill does not keep the stored why it read")
    if "exit" in WRITE_COLUMNS:
        raise SyncError("exit was stored as its own column")
    migration = MIGRATION_PATH.read_text(encoding="utf-8").lower()
    if "add column if not exists order_id" not in migration or "unique index" not in migration:
        raise SyncError("migration does not add a unique order_id")
    if "rh agentic (backfill|sync) order" not in migration:
        raise SyncError("migration does not copy tonight's backfill uuid onto order_id")
    workflow = (ROOT / ".github" / "workflows" / "export-kpi.yml").read_text(encoding="utf-8")
    if "repository_dispatch:" not in workflow or "rh-fill" not in workflow:
        raise SyncError("export workflow has no rh-fill ingest")
    if 'cron: "0 * * * *"' not in workflow:
        raise SyncError("export workflow has no hourly poll cron")
    if "Hourly Actions poll skipped" not in workflow:
        raise SyncError("hourly poll does not skip when Robinhood secrets are absent")
    if "ROBINHOOD_TOKEN:" in workflow:
        raise SyncError("export workflow requires a Robinhood token")
    if agentic_account({}) != DEFAULT_AGENTIC_ACCOUNT:
        raise SyncError("Agentic account default was not 546048042")
    if poll_start(Path("/no/such/rh-cursor.json")) != query_start(BOOTSTRAP_CURSOR):
        raise SyncError("missing cursor did not use the bootstrap")
    if query_start("2026-09-28T18:00:01Z") != "2026-09-28T12:00:01Z":
        raise SyncError("cursor overlap was not 6 hours")
    listed = orders_path(DEFAULT_AGENTIC_ACCOUNT, "2026-09-26T00:00:00Z")
    if "state=filled" not in listed or DEFAULT_AGENTIC_ACCOUNT not in listed:
        raise SyncError("orders path is not a filled-order GET for the Agentic account")
    try:
        sync(from_rh=True, env={}, default_feed=Path("/no/such/rh_fills.json"))
    except SyncError as exc:
        text = str(exc)
        if "RH_API_KEY" not in text or "ROBINHOOD_TOKEN is not used" not in text:
            raise
        if "read-only" in text.lower() or "disk full" in text.lower():
            raise SyncError("missing-secret copy must not look like a warehouse outage")
    else:
        raise SyncError("hourly poll without secrets did not fail when explicitly requested")
    message = request_message(DOC_API_KEY, DOC_TIMESTAMP, DOC_PATH, "GET")
    if message != f"{DOC_API_KEY}{DOC_TIMESTAMP}{DOC_PATH}GET":
        raise SyncError("GET signature message")
    try:
        import nacl.signing  # noqa: F401

        signing = True
    except ImportError:
        try:
            import cryptography.hazmat.primitives.asymmetric.ed25519  # noqa: F401

            signing = True
        except ImportError:
            signing = False
    if signing:
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
    absent = Path("/no/such/rh_fills.json")
    if feed_requested({}, None, absent):
        raise SyncError("missing feed looked requested")
    if feed_requested({"ROBINHOOD_TOKEN": "not-a-feed"}, None, absent):
        raise SyncError("ROBINHOOD_TOKEN was treated as a fills feed")
    if sync(env={"ROBINHOOD_TOKEN": "not-a-feed"}, default_feed=absent) != 0:
        raise SyncError("missing feed did not skip")
    try:
        load_fills("/no/such/fills.json")
    except SyncError as exc:
        if "not found" not in str(exc).lower():
            raise
    else:
        raise SyncError("missing --from-json file did not fail")
    try:
        sync(env={"SYNC_RH_JSON": "not-json-and-not-a-file"}, default_feed=absent)
    except SyncError as exc:
        if "not found" not in str(exc).lower() and "parsed" not in str(exc).lower():
            raise
    else:
        raise SyncError("bad SYNC_RH_JSON did not fail")
    inline = json.dumps({"results": fixture_orders()[:1]})
    if not feed_requested({"SYNC_RH_JSON": inline}, None, absent):
        raise SyncError("SYNC_RH_JSON was not a requested feed")
    try:
        sync(env={"SYNC_RH_JSON": "[]"}, default_feed=absent)
    except SyncError as exc:
        if "SUPABASE_SERVICE_ROLE_KEY" not in str(exc):
            raise
    else:
        raise SyncError("requested sync without supabase did not fail")
    bad_copy = "fills JSON could not be parsed. kpi_trades was not changed."
    if "read-only" in bad_copy.lower() or "disk full" in bad_copy.lower():
        raise SyncError("bad-input copy must not look like a warehouse outage")
    from tempfile import TemporaryDirectory

    with TemporaryDirectory() as tmp:
        target = Path(tmp)
        (target / "kpi_summary.json").write_text('[{"sleeve":"crypto"}]\n', encoding="utf-8")
        (target / "meta.json").write_text(
            json.dumps({"source": "supabase", "fetched_at": "2026-09-28T01:08:27Z"}) + "\n",
            encoding="utf-8",
        )
        stamp_sync_failure(bad_copy, target)
        summary = (target / "kpi_summary.json").read_text(encoding="utf-8")
        meta = json.loads((target / "meta.json").read_text(encoding="utf-8"))
    if summary != '[{"sleeve":"crypto"}]\n':
        raise SyncError("stamp rewrote KPI JSON")
    if meta.get("export_status") != "error" or "parsed" not in meta.get("export_error", ""):
        raise SyncError("stamp missed the bad-input error")
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
    print("self-test ok")
    return 0


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
        help="Print mapped rows and do not call Supabase",
    )
    parser.add_argument(
        "--from-json",
        metavar="PATH",
        help="Filled orders JSON. Use - to read stdin.",
    )
    parser.add_argument(
        "--print-cursor",
        action="store_true",
        help="Print updated_at_gte for the next MCP get_crypto_orders poll.",
    )
    parser.add_argument(
        "--from-rh",
        action="store_true",
        help="GET filled orders when RH_API_KEY and RH_BASE64_PRIVATE_KEY are set.",
    )
    args = parser.parse_args(argv)
    try:
        if args.self_test:
            return self_test()
        if args.print_cursor:
            print(poll_start())
            return 0
        if args.dry_run:
            orders = load_fills(args.from_json) if args.from_json else fixture_orders()
            print(json.dumps(rows_from_orders(orders, []), indent=2))
            print("dry-run: no upsert", file=sys.stderr)
            return 0
        return sync(from_json=args.from_json, from_rh=args.from_rh)
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
