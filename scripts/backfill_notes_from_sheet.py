#!/usr/bin/env python3
"""One-shot backfill of kpi_trades why/notes from a Google Sheet CSV.

Not part of Export KPI. The sheet is not a live source. Export regenerates
running P&L from the warehouse and does not read this file or the sheet.

Dry-run is the default. It prints planned why/notes updates and writes
nothing. `--dry-run` does the same thing when it is passed with `--apply`.
`--apply` writes, and only when SUPABASE_SERVICE_ROLE_KEY or SUPABASE_DB_URL
is set. Without those credentials the script exits 2 and writes nothing.

Sheet (human notes only):
  https://docs.google.com/spreadsheets/d/14o00TKyylOQYFHecAQ5RMlhYCDEOkxaJXrj1lGrtceo
  Tabs: Crypto, Equities. Columns include timestamp_ET, ticker, side, qty,
  why, order_id, notes.

Eng exports each tab to CSV (File → Download, or the connector) and runs:

  python3 scripts/backfill_notes_from_sheet.py --self-test
  python3 scripts/backfill_notes_from_sheet.py \\
    --csv crypto.csv --sleeve crypto \\
    --csv equities.csv --sleeve equities \\
    --dry-run
  SUPABASE_URL=https://bsnqwgbshwszbjncglqx.supabase.co \\
  SUPABASE_SERVICE_ROLE_KEY=... \\
  python3 scripts/backfill_notes_from_sheet.py \\
    --csv crypto.csv --sleeve crypto \\
    --csv equities.csv --sleeve equities \\
    --apply

Project: agentic-signals (bsnqwgbshwszbjncglqx).

Match order:
  1. Sheet order_id equals kpi_trades.order_id, or a UUID still stored in why.
  2. Else sleeve + ticker + side + qty + timestamp within 120 seconds.
     Sheet timestamps are America/New_York (Excel serial or clock time).

Updates only why and notes, and only when the sheet cell is a human note.
A human note is non-empty and is not `RH Agentic backfill|sync order <uuid>`.
Sheet human why replaces warehouse why, including a machine stub. Sheet notes
fill why when the sheet why cell is empty. Rows the sheet does not mention
are still updated when warehouse notes is human and why is a machine stub:
notes is copied onto why. Rows that do not match, or match more than one
fill, are skipped. Qty, price, and pnl are not written.

`--apply` uses SUPABASE_SERVICE_ROLE_KEY (REST PATCH) or SUPABASE_DB_URL.
"""

from __future__ import annotations

import argparse
import csv
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
from zoneinfo import ZoneInfo

DEFAULT_URL = "https://bsnqwgbshwszbjncglqx.supabase.co"
ET = ZoneInfo("America/New_York")
EXCEL_EPOCH = dt.datetime(1899, 12, 30, tzinfo=ET)
UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
MACHINE_WHY = re.compile(
    rf"^RH Agentic (?:backfill|sync) order {UUID_RE.pattern}$",
    re.IGNORECASE,
)
MATCH_WINDOW = dt.timedelta(seconds=120)
QTY_TOL = Decimal("0.00000001")
USER_AGENT = "the-book-notes-backfill/1"

SELECT_TRIES = (
    "id,sleeve,timestamp_et,ticker,side,qty,why,notes,order_id",
    "sleeve,timestamp_et,ticker,side,qty,why,notes,order_id",
    "id,sleeve,timestamp_et,ticker,side,qty,why,notes",
    "sleeve,timestamp_et,ticker,side,qty,why,notes",
    "id,sleeve,timestamp_et,ticker,side,qty,why,order_id",
    "sleeve,timestamp_et,ticker,side,qty,why,order_id",
    "id,sleeve,timestamp_et,ticker,side,qty,why",
    "sleeve,timestamp_et,ticker,side,qty,why",
)


class BackfillError(RuntimeError):
    """A backfill that must not exit 0."""


def human_note(value) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or MACHINE_WHY.match(text):
        return None
    return text


def parse_qty(value) -> Decimal | None:
    if value is None:
        return None
    text = str(value).strip().replace(",", "").replace("$", "")
    if text == "":
        return None
    try:
        return Decimal(text)
    except Exception:
        return None


def qty_close(left, right) -> bool:
    a = parse_qty(left)
    b = parse_qty(right)
    if a is None or b is None:
        return False
    return abs(a - b) <= QTY_TOL


def parse_sheet_time(value) -> dt.datetime | None:
    """Sheet timestamp_ET → UTC. Naive clocks are America/New_York."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    serial_text = text.replace(",", "")
    try:
        serial = float(serial_text)
    except ValueError:
        serial = None
    if serial is not None and serial > 20000:
        moment = EXCEL_EPOCH + dt.timedelta(days=serial)
        return moment.astimezone(dt.timezone.utc)
    cleaned = re.sub(r"\s+ET$", "", text, flags=re.IGNORECASE).strip()
    cleaned = cleaned.replace("Z", "+00:00")
    if " " in cleaned and "T" not in cleaned:
        cleaned = cleaned.replace(" ", "T", 1)
    try:
        parsed = dt.datetime.fromisoformat(cleaned)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ET)
    return parsed.astimezone(dt.timezone.utc)


def parse_warehouse_time(value) -> dt.datetime | None:
    if value is None:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    if " " in text and "T" not in text:
        text = text.replace(" ", "T", 1)
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def header_map(fieldnames: list[str] | None) -> dict[str, str]:
    aliases = {
        "timestamp_et": "timestamp",
        "timestamp": "timestamp",
        "time": "timestamp",
        "ticker": "ticker",
        "symbol": "ticker",
        "side": "side",
        "qty": "qty",
        "quantity": "qty",
        "why": "why",
        "notes/why": "why",
        "order_id": "order_id",
        "order id": "order_id",
        "notes": "notes",
        "note": "notes",
    }
    mapped = {}
    for name in fieldnames or []:
        key = aliases.get(str(name).strip().lower())
        if key and key not in mapped:
            mapped[key] = name
    return mapped


def read_sheet_csv(path: Path, sleeve: str) -> list[dict]:
    sleeve_name = sleeve.strip().lower()
    if sleeve_name not in {"crypto", "equities"}:
        raise BackfillError(f"sleeve must be crypto or equities, got {sleeve!r}")
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        columns = header_map(reader.fieldnames)
        missing = [name for name in ("timestamp", "ticker", "side") if name not in columns]
        if missing:
            raise BackfillError(f"{path} is missing columns {missing}")
        rows = []
        for raw in reader:
            ticker = str(raw.get(columns["ticker"]) or "").strip().upper()
            side = str(raw.get(columns["side"]) or "").strip().lower()
            if side not in {"buy", "sell"} or not ticker:
                continue
            why = human_note(raw.get(columns["why"])) if "why" in columns else None
            notes = human_note(raw.get(columns["notes"])) if "notes" in columns else None
            order_raw = str(raw.get(columns["order_id"]) or "").strip().lower() if "order_id" in columns else ""
            order_id = order_raw if UUID_RE.fullmatch(order_raw) else ""
            when = parse_sheet_time(raw.get(columns["timestamp"]))
            qty = parse_qty(raw.get(columns["qty"])) if "qty" in columns else None
            if why is None and notes is None:
                continue
            rows.append(
                {
                    "sleeve": sleeve_name,
                    "ticker": ticker,
                    "side": side,
                    "qty": qty,
                    "timestamp": when,
                    "order_id": order_id,
                    "why": why,
                    "notes": notes,
                }
            )
        return rows


def uuids_in(value) -> set[str]:
    return {match.group(0).lower() for match in UUID_RE.finditer(str(value or ""))}


def planned_text(sheet_row: dict) -> dict:
    """Columns to write. Sheet why wins; notes fill why when why is not human."""
    why = sheet_row.get("why")
    notes = sheet_row.get("notes")
    payload = {}
    if why:
        payload["why"] = why
    elif notes:
        payload["why"] = notes
    if notes:
        payload["notes"] = notes
    return payload


def match_trade(sheet_row: dict, trades: list[dict]):
    """Return the warehouse row, or None when unmatched or ambiguous."""
    order_id = sheet_row.get("order_id") or ""
    if order_id:
        hits = []
        for trade in trades:
            ids = uuids_in(trade.get("why"))
            if trade.get("order_id"):
                ids.add(str(trade["order_id"]).strip().lower())
            if order_id in ids and str(trade.get("sleeve") or "").strip().lower() == sheet_row["sleeve"]:
                hits.append(trade)
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            return None
    when = sheet_row.get("timestamp")
    qty = sheet_row.get("qty")
    if when is None or qty is None:
        return None
    hits = []
    for trade in trades:
        if str(trade.get("sleeve") or "").strip().lower() != sheet_row["sleeve"]:
            continue
        if str(trade.get("ticker") or "").strip().upper() != sheet_row["ticker"]:
            continue
        if str(trade.get("side") or "").strip().lower() != sheet_row["side"]:
            continue
        if not qty_close(trade.get("qty"), qty):
            continue
        trade_time = parse_warehouse_time(trade.get("timestamp_et"))
        if trade_time is None or abs(trade_time - when) > MATCH_WINDOW:
            continue
        hits.append(trade)
    if len(hits) == 1:
        return hits[0]
    return None


def build_plan(sheet_rows: list[dict], trades: list[dict]) -> tuple[list[dict], list[str]]:
    """Return updates and skip reasons. One warehouse row is updated at most once."""
    updates = []
    skips = []
    claimed = set()
    for index, sheet_row in enumerate(sheet_rows, start=1):
        payload = planned_text(sheet_row)
        if not payload:
            skips.append(f"row {index} {sheet_row['ticker']} has no human note")
            continue
        trade = match_trade(sheet_row, trades)
        if trade is None:
            skips.append(
                f"row {index} {sheet_row['sleeve']} {sheet_row['ticker']} {sheet_row['side']} unmatched or ambiguous"
            )
            continue
        key = trade_key(trade)
        if key in claimed:
            skips.append(f"row {index} {sheet_row['ticker']} repeats a fill already planned")
            continue
        changed = {}
        for column, value in payload.items():
            current = trade.get(column)
            current_text = "" if current is None else str(current).strip()
            if current_text == value:
                continue
            changed[column] = value
        if not changed:
            skips.append(f"row {index} {sheet_row['ticker']} already matches")
            continue
        claimed.add(key)
        updates.append({"trade": trade, "set": changed, "sheet": sheet_row})
    updates.extend(promote_notes_to_why(trades, claimed))
    return updates, skips


def promote_notes_to_why(trades: list[dict], claimed: set) -> list[dict]:
    """Copy human warehouse notes onto why when why is still a machine stub."""
    promoted = []
    for trade in trades:
        key = trade_key(trade)
        if key in claimed:
            continue
        notes = human_note(trade.get("notes"))
        why = "" if trade.get("why") is None else str(trade.get("why")).strip()
        if not notes or not MACHINE_WHY.match(why) or why == notes:
            continue
        claimed.add(key)
        promoted.append({"trade": trade, "set": {"why": notes}, "sheet": None})
    return promoted


def trade_key(trade: dict) -> tuple:
    if trade.get("id"):
        return ("id", str(trade["id"]))
    return (
        "natural",
        str(trade.get("sleeve") or "").lower(),
        str(trade.get("ticker") or "").upper(),
        str(trade.get("side") or "").lower(),
        str(trade.get("timestamp_et") or ""),
        str(trade.get("qty") or ""),
        str(trade.get("why") or ""),
    )


def rest_headers(key: str) -> dict[str, str]:
    return {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    }


def rest_call(base_url: str, key: str, path: str, method: str = "GET", body=None):
    data = None if body is None else json.dumps(body).encode("utf-8")
    headers = rest_headers(key)
    if body is not None:
        headers["Content-Type"] = "application/json"
        headers["Prefer"] = "return=representation"
    request = urllib.request.Request(
        base_url.rstrip("/") + path,
        data=data,
        method=method,
        headers=headers,
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:240]
        if key and key in detail:
            detail = detail.replace(key, "[redacted]")
        raise BackfillError(f"REST {method} kpi_trades HTTP {exc.code}: {detail}") from None
    if not raw:
        return []
    payload = json.loads(raw)
    if not isinstance(payload, list):
        raise BackfillError("REST kpi_trades did not return a row list")
    return payload


def fetch_trades_rest(base_url: str, key: str) -> tuple[list[dict], bool]:
    last_error = None
    for select in SELECT_TRIES:
        path = "/rest/v1/kpi_trades?select=" + urllib.parse.quote(select) + "&order=timestamp_et.asc"
        try:
            rows = rest_call(base_url, key, path)
        except BackfillError as exc:
            last_error = exc
            if "HTTP 400" in str(exc) or "42703" in str(exc) or "PGRST" in str(exc):
                continue
            raise
        notes_ok = "notes" in select
        return rows, notes_ok
    raise BackfillError(str(last_error) if last_error else "REST kpi_trades failed")


def fetch_trades_db(db_url: str) -> tuple[list[dict], bool]:
    try:
        import psycopg
    except ImportError as exc:
        raise BackfillError("psycopg is required for SUPABASE_DB_URL") from exc
    attempts = (
        (
            """
            select id, sleeve, timestamp_et, ticker, side, qty, why, notes, order_id
            from public.kpi_trades
            order by timestamp_et asc
            """,
            True,
        ),
        (
            """
            select id, sleeve, timestamp_et, ticker, side, qty, why, notes
            from public.kpi_trades
            order by timestamp_et asc
            """,
            True,
        ),
        (
            """
            select sleeve, timestamp_et, ticker, side, qty, why
            from public.kpi_trades
            order by timestamp_et asc
            """,
            False,
        ),
    )
    try:
        with psycopg.connect(db_url, connect_timeout=20) as conn:
            with conn.cursor() as cur:
                last_error = None
                for sql, notes_ok in attempts:
                    try:
                        cur.execute(sql)
                    except Exception as exc:
                        conn.rollback()
                        last_error = exc
                        continue
                    columns = [desc.name for desc in cur.description]
                    return [dict(zip(columns, row)) for row in cur.fetchall()], notes_ok
                raise BackfillError("database read of kpi_trades failed. No notes were written.") from last_error
    except BackfillError:
        raise
    except Exception as exc:
        raise BackfillError("database read of kpi_trades failed. No notes were written.") from exc


def patch_filter(trade: dict) -> str:
    if trade.get("id"):
        return "id=eq." + urllib.parse.quote(str(trade["id"]))
    parts = []
    for column in ("sleeve", "ticker", "side", "timestamp_et", "qty"):
        value = trade.get(column)
        if value is None or value == "":
            raise BackfillError(f"matched fill is missing {column}; not updating")
        parts.append(f"{column}=eq." + urllib.parse.quote(str(value)))
    if trade.get("why") is not None:
        parts.append("why=eq." + urllib.parse.quote(str(trade["why"])))
    return "&".join(parts)


def apply_rest(base_url: str, key: str, updates: list[dict], notes_ok: bool) -> int:
    written = 0
    for update in updates:
        body = dict(update["set"])
        if not notes_ok:
            body.pop("notes", None)
        if "why" not in body and "notes" not in body:
            continue
        path = "/rest/v1/kpi_trades?" + patch_filter(update["trade"])
        returned = rest_call(base_url, key, path, method="PATCH", body=body)
        if len(returned) != 1:
            raise BackfillError(
                f"PATCH matched {len(returned)} rows for {update['trade'].get('ticker')}; stopped"
            )
        written += 1
    return written


def apply_db(db_url: str, updates: list[dict], notes_ok: bool) -> int:
    try:
        import psycopg
    except ImportError as exc:
        raise BackfillError("psycopg is required for SUPABASE_DB_URL") from exc
    written = 0
    with psycopg.connect(db_url, connect_timeout=20) as conn:
        with conn.cursor() as cur:
            for update in updates:
                body = dict(update["set"])
                if not notes_ok:
                    body.pop("notes", None)
                if not body:
                    continue
                trade = update["trade"]
                assignments = []
                params: dict = {}
                for column, value in body.items():
                    assignments.append(f"{column} = %({column})s")
                    params[column] = value
                where = []
                if trade.get("id"):
                    where.append("id = %(id)s")
                    params["id"] = trade["id"]
                else:
                    for column in ("sleeve", "ticker", "side", "timestamp_et", "qty"):
                        where.append(f"{column} = %({column})s")
                        params[column] = trade[column]
                    if trade.get("why") is not None:
                        where.append("why = %(old_why)s")
                        params["old_why"] = trade["why"]
                cur.execute(
                    f"update public.kpi_trades set {', '.join(assignments)} where {' and '.join(where)}",
                    params,
                )
                if cur.rowcount != 1:
                    raise BackfillError(
                        f"UPDATE matched {cur.rowcount} rows for {trade.get('ticker')}; rolled back"
                    )
                written += 1
        conn.commit()
    return written


def format_update(update: dict) -> str:
    trade = update["trade"]
    bits = [
        f"{trade.get('sleeve')} {trade.get('ticker')} {trade.get('side')} {trade.get('timestamp_et')}"
    ]
    for column, value in update["set"].items():
        current = trade.get(column)
        bits.append(f"{column}: {current!r} -> {value!r}")
    return " | ".join(bits)


def self_test() -> int:
    avax_sheet_time = parse_sheet_time("46291.5255787037")
    expected = dt.datetime(2026, 9, 26, 16, 36, 50, tzinfo=dt.timezone.utc)
    if avax_sheet_time != expected:
        raise BackfillError(f"excel serial parsed {avax_sheet_time}")
    text_time = parse_sheet_time("2026-09-27 21:03 ET")
    if text_time != dt.datetime(2026, 9, 28, 1, 3, tzinfo=dt.timezone.utc):
        raise BackfillError(f"ET text parsed {text_time}")
    if human_note("RH Agentic backfill order 6ab7f4a2-5444-4593-84ea-e78f57dc0cf6"):
        raise BackfillError("machine why counted as human")
    if human_note("backfill from RH") != "backfill from RH":
        raise BackfillError("sheet placeholder was dropped")

    machine = "RH Agentic backfill order 6ab7f4a2-5444-4593-84ea-e78f57dc0cf6"
    trades = [
        {
            "id": "1",
            "sleeve": "crypto",
            "timestamp_et": "2026-09-26T16:36:50+00:00",
            "ticker": "AVAX",
            "side": "buy",
            "qty": "2.2528",
            "why": machine,
            "notes": None,
        },
        {
            "id": "2",
            "sleeve": "crypto",
            "timestamp_et": "2026-09-26T20:48:07+00:00",
            "ticker": "QNT",
            "side": "buy",
            "qty": "0.2066",
            "why": "RH Agentic sync order 6ab82f87-8b24-43ab-bd35-dc0099973cf4",
        },
        {
            "id": "3",
            "sleeve": "crypto",
            "timestamp_et": "2026-09-26T20:48:37+00:00",
            "ticker": "QNT",
            "side": "buy",
            "qty": "0.2062",
            "why": "RH Agentic sync order 6ab82fa5-063c-4170-81fe-063f93004a78",
        },
        {
            "sleeve": "equities",
            "timestamp_et": "2026-09-25T19:37:00+00:00",
            "ticker": "QCOM",
            "side": "buy",
            "qty": "0.743509",
            "why": "SWING unlock Joe/Wags; soft tgt flexible",
            "notes": "",
        },
        {
            "id": "4",
            "sleeve": "crypto",
            "timestamp_et": "2026-09-27T01:00:00+00:00",
            "ticker": "OP",
            "side": "buy",
            "qty": "3",
            "why": "RH Agentic sync order 6ab90000-0000-4000-8000-000000000004",
            "notes": "sized the dip",
            "order_id": "6ab90000-0000-4000-8000-000000000004",
        },
        {
            "id": "5",
            "sleeve": "crypto",
            "timestamp_et": "2026-09-27T02:00:00+00:00",
            "ticker": "RENDER",
            "side": "buy",
            "qty": "1.5",
            "why": None,
            "notes": None,
            "order_id": "6ab90000-0000-4000-8000-000000000005",
        },
    ]
    sheet_rows = [
        {
            "sleeve": "crypto",
            "ticker": "AVAX",
            "side": "buy",
            "qty": Decimal("2.2528"),
            "timestamp": avax_sheet_time,
            "order_id": "6ab7f4a2-5444-4593-84ea-e78f57dc0cf6",
            "why": "backfill from RH",
            "notes": "backfill from RH",
        },
        {
            "sleeve": "crypto",
            "ticker": "QNT",
            "side": "buy",
            "qty": Decimal("0.2066"),
            "timestamp": parse_sheet_time("2026-09-26 16:48"),
            "order_id": "",
            "why": "backfill from RH",
            "notes": None,
        },
        {
            "sleeve": "crypto",
            "ticker": "QNT",
            "side": "buy",
            "qty": None,
            "timestamp": parse_sheet_time("2026-09-26 16:48"),
            "order_id": "",
            "why": "should not match both",
            "notes": None,
        },
        {
            "sleeve": "equities",
            "ticker": "QCOM",
            "side": "buy",
            "qty": Decimal("0.743509"),
            "timestamp": parse_sheet_time("46290.65069444444"),
            "order_id": "6ab6cd5e-c83f-4429-a7c0-cf8e6d2fa953",
            "why": "SWING unlock Joe/Wags; soft tgt flexible",
            "notes": "First live fill.",
        },
        {
            "sleeve": "crypto",
            "ticker": "AVAX",
            "side": "buy",
            "qty": Decimal("2.2528"),
            "timestamp": avax_sheet_time,
            "order_id": "6ab7f4a2-5444-4593-84ea-e78f57dc0cf6",
            "why": "backfill from RH",
            "notes": "backfill from RH",
        },
        {
            "sleeve": "crypto",
            "ticker": "NOPE",
            "side": "buy",
            "qty": Decimal("1"),
            "timestamp": avax_sheet_time,
            "order_id": "",
            "why": "missing fill",
            "notes": None,
        },
        {
            "sleeve": "crypto",
            "ticker": "RENDER",
            "side": "buy",
            "qty": None,
            "timestamp": None,
            "order_id": "6ab90000-0000-4000-8000-000000000005",
            "why": "breakout add",
            "notes": "breakout add",
        },
    ]
    updates, skips = build_plan(sheet_rows, trades)
    by_id = {update["trade"].get("id"): update for update in updates if update["trade"].get("id")}
    if set(by_id) != {"1", "2", "4", "5"}:
        raise BackfillError(f"planned ids {set(by_id)} skips {skips}")
    if by_id["1"]["set"] != {"why": "backfill from RH", "notes": "backfill from RH"}:
        raise BackfillError(f"AVAX payload {by_id['1']['set']}")
    if "qty" in by_id["1"]["set"] or "pnl_trade_usd" in by_id["1"]["set"]:
        raise BackfillError("backfill tried to write qty or pnl")
    if by_id["2"]["set"] != {"why": "backfill from RH"}:
        raise BackfillError(f"QNT payload {by_id['2']['set']}")
    if not any("unmatched or ambiguous" in skip for skip in skips):
        raise BackfillError(f"ambiguous row was not skipped: {skips}")
    if not any("already planned" in skip for skip in skips):
        raise BackfillError(f"duplicate AVAX was not skipped: {skips}")
    qcom = next(update for update in updates if update["trade"]["ticker"] == "QCOM")
    if qcom["set"] != {"notes": "First live fill."}:
        raise BackfillError(f"QCOM payload {qcom['set']}")
    if by_id["4"]["set"] != {"why": "sized the dip"}:
        raise BackfillError(f"notes copy {by_id['4']['set']}")
    if by_id["5"]["set"] != {"why": "breakout add", "notes": "breakout add"}:
        raise BackfillError(f"order_id match {by_id['5']['set']}")

    link_id = "6ab90000-0000-4000-8000-000000000009"
    sheet_wins, _sheet_skips = build_plan(
        [
            {
                "sleeve": "crypto",
                "ticker": "LINK",
                "side": "buy",
                "qty": Decimal("1"),
                "timestamp": None,
                "order_id": link_id,
                "why": "sheet thesis",
                "notes": "sheet detail",
            }
        ],
        [
            {
                "id": "9",
                "sleeve": "crypto",
                "timestamp_et": "2026-09-27T03:00:00+00:00",
                "ticker": "LINK",
                "side": "buy",
                "qty": "1",
                "why": f"RH Agentic sync order {link_id}",
                "notes": "warehouse note",
                "order_id": link_id,
            }
        ],
    )
    if len(sheet_wins) != 1 or sheet_wins[0]["set"].get("why") != "sheet thesis":
        raise BackfillError(f"sheet why lost to warehouse notes {sheet_wins}")
    if sheet_wins[0]["set"].get("notes") != "sheet detail":
        raise BackfillError(f"sheet notes {sheet_wins[0]['set']}")
    lonely_id = "6ab90000-0000-4000-8000-000000000010"
    untouched, _lonely_skips = build_plan(
        [],
        [
            {
                "id": "10",
                "sleeve": "crypto",
                "timestamp_et": "2026-09-27T04:00:00+00:00",
                "ticker": "DOT",
                "side": "buy",
                "qty": "1",
                "why": f"RH Agentic sync order {lonely_id}",
                "notes": None,
                "order_id": lonely_id,
            }
        ],
    )
    if untouched:
        raise BackfillError(f"null notes cleared a stub {untouched}")
    kept_human, _kept_skips = build_plan(
        [],
        [
            {
                "id": "11",
                "sleeve": "equities",
                "timestamp_et": "2026-09-25T19:37:00+00:00",
                "ticker": "QCOM",
                "side": "buy",
                "qty": "1",
                "why": "SWING unlock Joe/Wags; soft tgt flexible",
                "notes": "First live fill.",
            }
        ],
    )
    if kept_human:
        raise BackfillError(f"human why was rewritten from notes {kept_human}")

    from tempfile import TemporaryDirectory

    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "crypto.csv"
        path.write_text(
            "timestamp_ET,ticker,side,qty,why,order_id,notes\n"
            "46291.5255787037,AVAX,buy,2.2528,backfill from RH,"
            "6ab7f4a2-5444-4593-84ea-e78f57dc0cf6,backfill from RH\n"
            "OpenLots refresh,SEI,192.58,cost,ignore,,\n",
            encoding="utf-8",
        )
        parsed = read_sheet_csv(path, "crypto")
    if len(parsed) != 1 or parsed[0]["ticker"] != "AVAX" or parsed[0]["order_id"].startswith("6ab7") is False:
        raise BackfillError(f"csv parse {parsed}")
    print("self-test ok")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--csv", action="append", default=[], help="CSV path. Repeat with --sleeve in the same order.")
    parser.add_argument(
        "--sleeve",
        action="append",
        default=[],
        help="Sleeve for the matching --csv (crypto or equities).",
    )
    parser.add_argument("--apply", action="store_true", help="Write why/notes. Default is dry-run.")
    parser.add_argument("--dry-run", action="store_true", help="Print the plan and do not write.")
    args = parser.parse_args(argv)
    if args.self_test:
        return self_test()
    if len(args.csv) == 0 or len(args.csv) != len(args.sleeve):
        print("Pass one --sleeve for each --csv, or --self-test.", file=sys.stderr)
        return 2
    sheet_rows = []
    for path, sleeve in zip(args.csv, args.sleeve):
        sheet_rows.extend(read_sheet_csv(Path(path), sleeve))
    key = (os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or "").strip()
    db_url = (os.environ.get("SUPABASE_DB_URL") or "").strip()
    base_url = (os.environ.get("SUPABASE_URL") or DEFAULT_URL).strip()
    if not key and not db_url:
        print(
            "No SUPABASE_SERVICE_ROLE_KEY or SUPABASE_DB_URL. "
            "Refusing to guess matches against an empty warehouse.",
            file=sys.stderr,
        )
        return 2
    if key:
        try:
            trades, notes_ok = fetch_trades_rest(base_url, key)
            via = "rest"
        except BackfillError:
            if not db_url:
                raise
            trades, notes_ok = fetch_trades_db(db_url)
            via = "db"
    else:
        trades, notes_ok = fetch_trades_db(db_url)
        via = "db"
    updates, skips = build_plan(sheet_rows, trades)
    print(f"Loaded {len(trades)} kpi_trades via {via}. Sheet trade rows with notes: {len(sheet_rows)}.")
    print(f"Planned updates: {len(updates)}. Skipped: {len(skips)}.")
    for skip in skips:
        print(f"skip: {skip}")
    for update in updates:
        print(f"update: {format_update(update)}")
    if not notes_ok:
        print("kpi_trades.notes is not readable. why will be updated; notes will be left alone.")
    if not args.apply or args.dry_run:
        print("Dry run. Re-run with --apply to write why/notes.")
        return 0
    if via == "rest" and key:
        written = apply_rest(base_url, key, updates, notes_ok)
    else:
        written = apply_db(db_url, updates, notes_ok)
    print(f"Updated {written} kpi_trades rows.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BackfillError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
