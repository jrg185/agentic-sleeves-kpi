# Agentic sleeves KPI

Read-only dashboard for the Agentic sleeves book: **Crypto**, **Equities**, and **combined**.

The page is static. It does not place orders, and it does not call Supabase from the browser.

- Repo: https://github.com/jrg185/agentic-sleeves-kpi
- Pages: https://jrg185.github.io/agentic-sleeves-kpi/

## What the board shows

For each sleeve and the combined book:

- Start (the sleeve seed)
- Running balance
- P&L in dollars and percent
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

Workflow source: [`scripts/export-kpi.yml`](scripts/export-kpi.yml)

GitHub only runs a workflow from `.github/workflows/`. Copy that file to `.github/workflows/export-kpi.yml` with a token that can write workflow files, then run **Actions → Export KPI**.

- `workflow_dispatch`, and also when the workflow file or exporter script is pushed to `main`
- Reads `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY` from Actions secrets
- Writes `data/*.json` and commits the JSON when it changed
- Does not deploy Pages and does not change the Pages source

There is no Sheets API key and no Google CSV export in this path. A sheet may feed Supabase somewhere else; this site does not.

### View contract

`kpi_summary` rows:

| Column | Meaning |
| --- | --- |
| `sleeve` | `crypto`, `equities`, or `combined` |
| `as_of` | Snapshot timestamp |
| `running_balance_frac` | Running balance ÷ seed |
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
| `running_balance_frac` | Running balance ÷ sleeve seed |
| `why` | Short reason. No PII. |

The exporter drops `email`, `phone`, `order_id`, `account_id`, `user_id`, `api_key`, `service_role`, `secret`, `password`, `token`, `ssn`, and `address` if a view ever returns them. It also drops JWT-shaped strings.

`models_oos` is optional. The exporter does not fail the job when that view is absent.

The Models tab reads `data/models.json` (same shape in `fixtures/models.json`). Each card has `name`, `sleeve`, `used`, `training`, `data_source`, and `oos` (`window`, `hit_rate`, `avg_return`, `n`, `note`). Until T04 publishes metrics, `oos.status` is `placeholder` and the three numbers stay null. Replacing the file is enough; the page does not need a code change.

### Secrets

Until both Actions secrets are set, the site ships the sample in `data/` (same bytes as `fixtures/`). `meta.json` says `"source": "sample"`.

Add these repository secrets (Settings → Secrets and variables → Actions). Do not commit them. Do not put them in client JavaScript.

| Secret | Use |
| --- | --- |
| `SUPABASE_URL` | `https://bsnqwgbshwszbjncglqx.supabase.co` |
| `SUPABASE_SERVICE_ROLE_KEY` | PostgREST `GET /rest/v1/<view>?select=*` with `apikey` and `Authorization: Bearer`. Used for `kpi_summary`, `kpi_trades_scrubbed`, and `models_oos` when that view exists. |

After the secrets are saved, run **Actions → Export KPI → Run workflow**. A successful export sets `meta.source` to `supabase` and replaces `data/*.json`. `fixtures/` stays the sample.

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
