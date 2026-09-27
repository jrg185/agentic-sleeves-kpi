# Agentic sleeves KPI

Read-only dashboard for the Agentic sleeves book: **Crypto**, **Equities**, and **combined**.

The page is static. It does not place orders, and it does not call Supabase from the browser.

- Repo: https://github.com/jrg185/agentic-sleeves-kpi
- Pages: https://jrg185.github.io/agentic-sleeves-kpi/

## What the board shows

For each sleeve and the combined book:

- Start (the sleeve seed)
- Realized P&L (closed exits only) in dollars and percent
- Unrealized P&L (open mark-to-market versus cost) in dollars and percent
- Running balance (book): start + realized + unrealized. The fraction is book ÷ seed. It is not an unlabeled blend of the two P&L figures, and it is not cash left after a fill.
- Day P&L
- Day kill rail (percent of book, and the dollar size of that rail)
- Kill headroom (percent of book still inside the rail, and dollars)
- Crypto day target, marked realized-only

Dollar figures are **seed × fraction**. The scrubbed JSON does not need raw book dollars.

| Sleeve | Seed used when the row has no start/seed |
| --- | --- |
| Crypto | $300 |
| Equities | $500 |
| Combined | $800 |

Rails encoded in the sample (fractions of that seed):

- Crypto day kill `-0.10` (−10%, −$30) and day target `0.025` (+2.5%, +$7.50, realized only)
- Equities day kill `-0.25` (−25%, −$125)
- Combined kill headroom `155/800` of book. The kill percent itself stays per sleeve.

## Data path

Source of truth is the Supabase project **agentic-signals** (`bsnqwgbshwszbjncglqx`).

```
https://bsnqwgbshwszbjncglqx.supabase.co
```

GitHub Actions reads these scrubbed views and writes JSON into the repo:

- `public.kpi_summary` → `data/kpi_summary.json`
- `public.kpi_trades_scrubbed` → `data/kpi_trades_scrubbed.json`
- `public.models_oos` → `data/models_oos.json` when that view exists
- provenance → `data/meta.json`

Pages serves that committed JSON. The browser only fetches `data/*.json`.

Workflow: [`.github/workflows/export-kpi.yml`](.github/workflows/export-kpi.yml) (source copy: [`scripts/export-kpi.yml`](scripts/export-kpi.yml)).

- `workflow_dispatch`, and also when the workflow file or exporter script is pushed to `main`
- Reads `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY` from Actions secrets
- Writes `data/kpi_summary.json`, `data/kpi_trades_scrubbed.json`, `data/models_oos.json`, and `data/meta.json` when they changed
- Does not read `ALPHA_VANTAGE_API_KEY` or `FINNHUB_API_KEY`
- Does not rewrite `data/models.json`
- Does not deploy Pages and does not change the Pages source

There is no Sheets API key and no Google CSV export in this path. A sheet may feed Supabase somewhere else; this site does not.

The live `kpi_summary` view uses warehouse names. Export remaps them onto the page shape before writing JSON: `running_bal_vs_start` → `running_balance_frac`, `pnl_pct_of_book` → `running_pnl_frac`, `notes` → `note`. `sleeve`, `as_of`, `day_kill_pct`, `day_target_pct`, and `kill_headroom_frac` stay as they are. When both a warehouse book ratio and a cash residual are present, the warehouse ratio wins.

### View contract

`kpi_summary` rows:

| Column | Meaning |
| --- | --- |
| `sleeve` | `crypto`, `equities`, or `combined` |
| `as_of` | Snapshot timestamp |
| `running_balance_frac` | Sleeve book ÷ seed. Sheet desks: crypto 1.079233 ($323.77 / $300, realized +$6.24), equities 1.00174 ($500.87 / $500), combined 1.0308 ($824.64 / $800). |
| `running_pnl_frac` | Running P&L ÷ seed. Tape column only. Sleeve cards do not show this as an unlabeled P&L. |
| `realized_pnl_frac` | Closed-exit P&L ÷ seed. Also accepted: `realized_pnl_pct`, `realized_pnl_pct_of_book`, `realized_pct_of_book`, `rpnl_frac`. |
| `unrealized_pnl_frac` | Open mark-to-market P&L ÷ seed. Also accepted: `unrealized_pnl_pct`, `unrealized_pnl_pct_of_book`, `unrealized_pct_of_book`, `upnl_frac`. |
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
| `SUPABASE_SERVICE_ROLE_KEY` | PostgREST `GET /rest/v1/<view>?select=*` with `apikey` and `Authorization: Bearer`. Used for `kpi_summary`, `kpi_trades_scrubbed`, and `models_oos` when that view exists. Set this the same way as the other Actions secrets. Until it is set, the board keeps the committed sample. |

`ALPHA_VANTAGE_API_KEY` and `FINNHUB_API_KEY` are already on this repository. Export KPI does not read them.

After the Supabase secrets are saved, run **Actions → Export KPI → Run workflow**. A successful export sets `meta.source` to `supabase` and replaces the KPI JSON. `fixtures/` and `data/models.json` stay as they are.

If the service role secret is unset, `scripts/export_kpi.py` exits 0 and leaves the committed JSON alone.

## GitHub Pages

Pages is a legacy site: branch `main`, path `/` (repository root). Pushing `index.html`, `styles.css`, `app.js`, and `data/*.json` to `main` publishes them. Do not switch the source to GitHub Actions.

The site is https://jrg185.github.io/agentic-sleeves-kpi/

## Local preview

```bash
python3 scripts/export_kpi.py --install-sample
python3 -m unittest scripts/test_kpi.py
python3 -m http.server 8765
```

Open http://127.0.0.1:8765/
