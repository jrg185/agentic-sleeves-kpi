-- Idempotency key for Robinhood Agentic crypto fills on public.kpi_trades.
-- Legacy rows stay valid: order_id is nullable, and Postgres unique allows many nulls.
-- Apply once before Export KPI's sync cron. scripts/sync_rh_kpi_trades.py also
-- runs this file when SUPABASE_DB_URL is set.
--
--   psql "$SUPABASE_DB_URL" -v ON_ERROR_STOP=1 -f scripts/migrate_kpi_trades_order_id.sql

alter table public.kpi_trades
  add column if not exists order_id text;

-- Tonight's one-shot backfill stored the uuid in why
-- ("RH Agentic backfill order <uuid>"), not in a column.
update public.kpi_trades
set order_id = lower((regexp_match(
  why,
  '([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})'
))[1])
where order_id is null
  and why ~* 'RH Agentic (backfill|sync) order [0-9a-fA-F]{8}-';

create unique index if not exists kpi_trades_order_id_key
  on public.kpi_trades (order_id);
