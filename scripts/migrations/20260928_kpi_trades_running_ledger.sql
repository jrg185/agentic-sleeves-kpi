-- Tape running ledger for public.kpi_trades_scrubbed.
--
-- Apply once on Supabase project agentic-signals (bsnqwgbshwszbjncglqx):
--   Dashboard → SQL editor → paste this file → Run.
-- Then NOTIFY reloads the PostgREST schema cache (included below).
-- Then GitHub Actions → Export KPI. Hard-refresh Pages.
--
-- This does not insert fills, does not place orders, and does not read a sheet.
-- Sibling RH sync owns order_id / why on new rows. This only replaces the view.
--
-- Running realized P&L after a fill is sum(pnl_trade_usd) / sleeve seed.
-- Running book is (seed + that sum) / seed. Seeds: crypto 300, equities 500.
-- Book here is start + cumulative realized P&L, not cash left after the fill
-- and not mark-to-market. why is the full note (no left()).
--
-- A machine why (`RH Agentic backfill|sync order <uuid>`) is replaced in the
-- view by notes when notes is a human sentence. The one-shot
-- scripts/backfill_notes_from_sheet.py writes those columns on kpi_trades.
--
-- If this script created public.kpi_trades_scrubbed_prev and the new view is
-- wrong, restore with (prev is the old SELECT, not a wrapper of the new view):
--   drop view public.kpi_trades_scrubbed;
--   alter view public.kpi_trades_scrubbed_prev rename to kpi_trades_scrubbed;
--   notify pgrst, 'reload schema';
-- A failure inside this transaction rolls back. The live view stays as it was.
--
-- Check after apply (last crypto fill should be non-null):
--   select sleeve, timestamp_et, ticker, side,
--          running_pnl_frac, running_balance_frac, why
--   from public.kpi_trades_scrubbed
--   where lower(sleeve) = 'crypto'
--   order by timestamp_et desc, ticker desc, side desc
--   limit 1;

begin;

do $mig$
declare
  notional_sql text;
  why_sql text;
  has_notes boolean;
  has_notional_usd boolean;
  has_notional boolean;
  prev_def text;
begin
  if to_regclass('public.kpi_trades') is null then
    raise exception 'public.kpi_trades is missing on this database';
  end if;
  if not exists (
    select 1
    from information_schema.columns
    where table_schema = 'public'
      and table_name = 'kpi_trades'
      and column_name = 'pnl_trade_usd'
  ) then
    raise exception 'public.kpi_trades.pnl_trade_usd is missing';
  end if;

  select exists (
    select 1 from information_schema.columns
    where table_schema = 'public' and table_name = 'kpi_trades' and column_name = 'notional_usd'
  ) into has_notional_usd;
  select exists (
    select 1 from information_schema.columns
    where table_schema = 'public' and table_name = 'kpi_trades' and column_name = 'notional'
  ) into has_notional;
  select exists (
    select 1 from information_schema.columns
    where table_schema = 'public' and table_name = 'kpi_trades' and column_name = 'notes'
  ) into has_notes;

  if has_notional_usd and has_notional then
    notional_sql := 'coalesce(t.notional_usd, t.notional, t.qty * t.avg_price)';
  elsif has_notional_usd then
    notional_sql := 'coalesce(t.notional_usd, t.qty * t.avg_price)';
  elsif has_notional then
    notional_sql := 'coalesce(t.notional, t.qty * t.avg_price)';
  else
    notional_sql := '(t.qty * t.avg_price)';
  end if;

  if has_notes then
    why_sql := $why$
      case
        when t.why ~* '^RH Agentic (backfill|sync) order [0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
          and nullif(btrim(t.notes), '') is not null
          and btrim(t.notes) !~* '^RH Agentic (backfill|sync) order [0-9a-f]{8}-'
        then t.notes
        when nullif(btrim(t.notes), '') is not null
          and t.why is not null
          and length(t.notes) > length(t.why)
          and left(t.notes, length(t.why)) = t.why
          and btrim(t.notes) !~* '^RH Agentic (backfill|sync) order [0-9a-f]{8}-'
        then t.notes
        else t.why
      end
    $why$;
  else
    why_sql := 't.why';
  end if;

  -- Copy the old SELECT (pg_get_viewdef), so the backup does not depend on
  -- the view we are about to replace. Restore:
  --   drop view public.kpi_trades_scrubbed;
  --   alter view public.kpi_trades_scrubbed_prev rename to kpi_trades_scrubbed;
  if to_regclass('public.kpi_trades_scrubbed') is not null
     and to_regclass('public.kpi_trades_scrubbed_prev') is null then
    prev_def := pg_get_viewdef('public.kpi_trades_scrubbed'::regclass, true);
    prev_def := rtrim(prev_def);
    if right(prev_def, 1) = ';' then
      prev_def := left(prev_def, length(prev_def) - 1);
    end if;
    raise notice 'previous public.kpi_trades_scrubbed definition: %', prev_def;
    execute 'create view public.kpi_trades_scrubbed_prev as ' || prev_def;
  end if;

  execute format($view$
    create or replace view public.kpi_trades_scrubbed as
    with fills as (
      select
        t.sleeve,
        t.timestamp_et,
        t.ticker,
        t.side,
        case lower(btrim(t.sleeve))
          when 'crypto' then 300::numeric
          when 'equities' then 500::numeric
        end as seed,
        (%s)::numeric as notional_usd,
        coalesce(t.pnl_trade_usd, 0)::numeric as pnl_usd,
        (%s)::text as why,
        t.ctid as row_ctid
      from public.kpi_trades t
    ),
    ledger as (
      select
        sleeve,
        timestamp_et,
        ticker,
        side,
        case
          when seed is null or seed = 0 or notional_usd is null then null
          else round(notional_usd / seed, 6)
        end as notional_frac_of_book,
        case
          when seed is null or seed = 0 then null
          else round(pnl_usd / seed, 6)
        end as pnl_frac_of_book,
        why,
        seed,
        sum(pnl_usd) over (
          partition by lower(btrim(sleeve))
          order by timestamp_et asc, ticker asc, side asc, row_ctid asc
          rows between unbounded preceding and current row
        ) as running_pnl_usd
      from fills
    )
    select
      sleeve,
      timestamp_et,
      ticker,
      side,
      notional_frac_of_book,
      pnl_frac_of_book,
      why,
      case
        when seed is null or seed = 0 then null
        else round(running_pnl_usd / seed, 6)
      end as running_pnl_frac,
      case
        when seed is null or seed = 0 then null
        else round((seed + running_pnl_usd) / seed, 6)
      end as running_balance_frac
    from ledger
  $view$, notional_sql, why_sql);
end
$mig$;

commit;

notify pgrst, 'reload schema';
