# The Book

Read-only dashboard for The Book: **Crypto**, **Equities**, and **combined**.

The page is static. It does not place orders, and it does not call Supabase from the browser.

- Repo: https://github.com/jrg185/the-book
- Pages: https://jrg185.github.io/the-book/

## What the board shows

For each desk and the combined book:

- Start (the book seed)
- Running balance (book): cash + mark-to-market of open positions. Until true MTM, book = start + running P&L, so the fraction is `(start + running_pnl) / start`. It is not cash left after a fill.
- P&L in dollars and percent
- Day P&L
- Day kill rail (percent of book, and the dollar size of that rail)
- Kill headroom (percent of book still inside the rail, and dollars)
- Crypto day target, marked realized-only

Dollar figures are **seed × fraction**. The scrubbed JSON does not need raw book dollars.

| Desk | Seed used when the row has no start/seed |
| --- | --- |
| Crypto | $300 |
| Equities | $500 |
| Combined | $800 |

Rails encoded in the sample (fractions of that seed):

- Crypto day kill `-0.10` (−10%, −$30) and day target `0.025` (+2.5%, +$7.50, realized only)
- Equities day kill `-0.25` (−25%, −$125)
- Combined kill headroom `155/800` of book. The kill percent itself stays per desk.

## Data path

Source of truth is the Supabase project **agentic-signals** (`bsnqwgbshwszbjncglqx`).

```
https://bsnqwgbshwszbjncglqx.supabase.co
```

`public.kpi_summary` is a view over the latest `public.kpi_sleeve_snapshots` row. Export only SELECTs that view, so it cannot move `as_of`. The Action runs `scripts/refresh_kpi_snapshots.py` first. That script reads `public.kpi_trades` (qty and price), marks open positions, and INSERTs a new snapshot per sleeve. It does not read `public.kpi_trades_scrubbed`.

GitHub Actions then writes the scrubbed views into the repo:

- `public.kpi_summary` → `data/kpi_summary.json`
- `public.kpi_trades_scrubbed` → `data/kpi_trades_scrubbed.json`
- `public.models_oos` → `data/models_oos.json` when that view exists
- provenance → `data/meta.json`

Pages serves that committed JSON. The browser only fetches `data/*.json`.

Workflow: [`.github/workflows/export-kpi.yml`](.github/workflows/export-kpi.yml) (same bytes as [`scripts/export-kpi.yml`](scripts/export-kpi.yml)).

- `workflow_dispatch`, pull requests (position math only), and pushes to `main` other than `data/**`
- schedule: every 15 minutes on weekdays from 13:00–21:45 UTC (covers 9:30am–4:00pm ET in both EDT and EST), and hourly outside that window including weekends
- Refresh `kpi_sleeve_snapshots`, then export. A failed refresh does not commit KPI JSON
- Reads `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY`. `SUPABASE_DB_URL` is optional
- Crypto marks: public Coinbase ticker, then Yahoo `{SYMBOL}-USD`. Equities marks: Finnhub when `FINNHUB_API_KEY` is set, then Yahoo chart. CoinStats and Alpha Vantage are later fallbacks when those keys are set
- Writes `data/kpi_summary.json`, `data/kpi_trades_scrubbed.json`, `data/models_oos.json`, and `data/meta.json` when they changed
- Does not rewrite `data/models.json`
- Does not deploy Pages and does not change the Pages source

The board reads `meta.fetched_at` as **Last refreshed** in America/New_York, and each sleeve `as_of` the same way. A healthy export sets `meta.export_status` to `ok`. If the service role key is missing, the script leaves the KPI files alone, sets `export_status` to `stale`, and exits 0. If the refresh throws (read-only filesystem, disk full, or a failed REST read), it stamps `export_status` `error` on `meta.json` only and exits 1. The workflow then commits that meta file and stays red. The page shows an **Export failed** or **Stale snapshot** chip plus the error copy, and it does not invent new KPI numbers. If a crash cannot write `meta.json`, the chip turns stale once `fetched_at` is older than 3 hours.

### Warehouse MTM

`public.kpi_summary` is a view over the latest `public.kpi_sleeve_snapshots` row. Re-exporting JSON cannot move `as_of` by itself.

The writer is in this repo: `scripts/refresh_kpi_snapshots.py`. Export KPI runs it first. It reads `public.kpi_trades` (qty and price), marks open names from public Coinbase and Yahoo quotes, and INSERTs a new snapshot with `as_of` set to now. It does not place orders and it does not change the table schema. Sibling `upsert-warehouse` Actions in `agentic-crypto-signals` and `agentic-equity-signals` write `bars`, `features`, `labels`, and `model_runs`. They are not the sleeve MTM writer.

Order:

1. Refresh inserts `kpi_sleeve_snapshots`.
2. Export reads `kpi_summary` and `kpi_trades_scrubbed` and commits JSON only if `as_of` is within 15 minutes.
3. Pages shows that `as_of`.

If the INSERT fails because the database is read-only (25006) or the disk is full, the script leaves the KPI numbers alone and stamps `meta.warehouse_status`. The chip reads **Warehouse read-only** or **Warehouse disk full**, with copy `snapshot frozen at` the last committed sleeve time. The Action publishes that meta file and stays red.

When `meta.source` is `supabase` and the latest sleeve `as_of` is older than 60 minutes, and the warehouse did not report read-only or disk full, the chip is **MTM stale**. Cache-busting the JSON does not make that snapshot current.

There is no Sheets API key and no Google CSV export in this path. A sheet may feed Supabase somewhere else; this site does not.

The live `kpi_summary` view uses warehouse names. Export remaps them onto the page shape before writing JSON: `running_bal_vs_start` → `running_balance_frac`, `pnl_pct_of_book` → `running_pnl_frac`, `notes` → `note`. `sleeve`, `as_of`, `day_kill_pct`, `day_target_pct`, and `kill_headroom_frac` stay as they are. When both a warehouse book ratio and a cash residual are present, the warehouse ratio wins.

### View contract

`kpi_summary` rows:

| Column | Meaning |
| --- | --- |
| `sleeve` | `crypto`, `equities`, or `combined` |
| `as_of` | Snapshot timestamp |
| `running_balance_frac` | Sleeve book ÷ seed. Sheet desks: crypto 1.079233 ($323.77 / $300, realized +$6.24), equities 1.00174 ($500.87 / $500), combined 1.0308 ($824.64 / $800). |
| `running_pnl_frac` | Running P&L ÷ seed |
| `day_pnl_frac` | Day P&L ÷ seed, or null |
| `day_kill_pct` | Kill rail as a fraction of book (`-0.10` = −10%). Percent points such as `-10` are also accepted. |
| `kill_headroom_frac` | Room left inside the kill rail, as a fraction of book |
| `day_target_pct` | Target as a fraction of book (`0.025` = +2.5%), or null |
| `note` | Short scrubbed note. No names, emails, or account ids. |

Optional seed override on a row: `start`, `seed`, `start_usd`, `seed_usd`, or `book_usd`. If none of those are present, the page uses the seeds above.

`kpi_trades_scrubbed` rows:

| Column | Meaning |
| --- | --- |
| `sleeve` | `crypto` or `equities` |
| `ts` | Fill time |
| `ticker` | Symbol |
| `side` | `buy` or `sell` |
| `qty` | Quantity |
| `pnl_frac` | Trade P&L ÷ sleeve seed |
| `running_pnl_frac` | Running P&L ÷ sleeve seed |
| `running_balance_frac` | Book at that fill ÷ sleeve seed, where book = start + running P&L at the row. Not cash leftover. |
| `why` | Short reason. No PII. |

The exporter drops `email`, `phone`, `order_id`, `account_id`, `user_id`, `api_key`, `service_role`, `secret`, `password`, `token`, `ssn`, and `address` if a view ever returns them. It also drops JWT-shaped strings.

`models_oos` is optional. The exporter does not fail the job when that view is absent.

The Models tab reads `data/models.json` (same shape in `fixtures/models.json`). Each card has `name`, `sleeve`, `used`, `training`, `data_source`, and `oos` (`window`, `hit_rate`, `avg_return`, `n`, `note`). Until T04 publishes metrics, `oos.status` is `placeholder` and the three numbers stay null. Replacing the file is enough; the page does not need a code change.

### Secrets

Until `SUPABASE_SERVICE_ROLE_KEY` is set, the site ships the sample in `data/` (same bytes as `fixtures/`). `meta.json` says `"source": "sample"`.

Add these repository secrets (Settings → Secrets and variables → Actions). Do not commit them. Do not put them in client JavaScript.

| Secret | Use |
| --- | --- |
| `SUPABASE_URL` | `https://bsnqwgbshwszbjncglqx.supabase.co` |
| `SUPABASE_SERVICE_ROLE_KEY` | PostgREST read of `kpi_trades` and INSERT into `kpi_sleeve_snapshots`, then `GET /rest/v1/<view>?select=*`. Until it is set, a local export leaves the committed JSON alone. The Action refresh step exits non-zero instead, so it does not commit a stale snapshot. |
| `SUPABASE_DB_URL` | Optional. Used when REST cannot read `kpi_trades` or the INSERT is rejected. |
| `FINNHUB_API_KEY` | Optional equities mark. Yahoo chart is the public fallback. |
| `COINSTATS_API_KEY` | Optional crypto mark after Coinbase and Yahoo. |
| `ALPHA_VANTAGE_API_KEY` | Optional equities mark after Finnhub and Yahoo. |

Public Coinbase and Yahoo marks do not need those quote keys.

After the Supabase secrets are saved, run **Actions → Export KPI → Run workflow**. A successful export sets `meta.source` to `supabase` and replaces the KPI JSON. `fixtures/` and `data/models.json` stay as they are.

If the service role secret is unset, a local `scripts/export_kpi.py` exits 0, leaves the KPI JSON alone, and marks `meta.export_status` as `stale`. `KPI_REFRESH_EXPECTED=1` (set on the export step after refresh) exits non-zero instead.

## GitHub Pages

Pages is a legacy site: branch `main`, path `/` (repository root). Pushing `index.html`, `styles.css`, `app.js`, and `data/*.json` to `main` publishes them. Do not switch the source to GitHub Actions.

The site is https://jrg185.github.io/the-book/

## Local preview

```bash
python3 scripts/refresh_kpi_snapshots.py --self-test
python3 scripts/export_kpi.py --install-sample
python3 -m py_compile scripts/export_kpi.py
python3 -m unittest scripts/test_export_status.py
python3 -m http.server 8765
```

Dry-run reads `kpi_trades` and prints the rows it would insert. It does not write:

```bash
SUPABASE_URL=https://bsnqwgbshwszbjncglqx.supabase.co \
SUPABASE_SERVICE_ROLE_KEY=... \
python3 scripts/refresh_kpi_snapshots.py --dry-run
```

After a live run, `as_of` should be the run time:

```sql
select sleeve, as_of, realized_pnl_usd, unrealized_pnl_usd,
       running_pnl_usd, running_balance_usd, start_balance_usd
from public.kpi_sleeve_snapshots
order by as_of desc
limit 6;
```

Open http://127.0.0.1:8765/
