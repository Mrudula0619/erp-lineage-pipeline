-- Rebuild core from one raw load. Re-runnable: core is a pure function of (raw load, rules below).
-- Every core row carries raw_id -> raw.records, which carries the exact API URL, page and position.
-- Usage: psql -v load_id=<uuid> -f 010_transform.sql
\set ON_ERROR_STOP on
begin;

delete from quarantine.records where load_id = :'load_id'::uuid;
delete from dq.recon_breaks    where load_id = :'load_id'::uuid;
truncate core.ledger_entries, core.invoice_lines, core.invoices, core.customers;

-- ---------------------------------------------------------------- customers
create temp table s_customers on commit drop as
select raw_id, source_id,
       payload->>'name'               as name,
       payload->>'region'             as region,
       (payload->>'created_at')::date as created_at,
       payload->>'status'             as status,
       row_number() over (partition by source_id order by page, position) as rn
from raw.records where load_id = :'load_id'::uuid and entity = 'customers';

insert into quarantine.records (load_id, entity, source_id, raw_id, reason, detail)
select :'load_id'::uuid, 'customers', source_id, raw_id, 'duplicate_source_id', 'repeat occurrence of the same id'
from s_customers where rn > 1;

insert into core.customers
select source_id, name, region, created_at, status, raw_id, :'load_id'::uuid
from s_customers where rn = 1;

-- ----------------------------------------------------------------- invoices
create temp table s_invoices on commit drop as
select raw_id, source_id,
       nullif(payload->>'customer_id', '') as customer_id,
       (payload->>'issued_on')::date       as issued_on,
       (payload->>'due_on')::date          as due_on,
       payload->>'currency'                as currency,
       (payload->>'total')::numeric        as total,
       payload->>'status'                  as status,
       row_number() over (partition by source_id order by page, position) as rn
from raw.records where load_id = :'load_id'::uuid and entity = 'invoices';

-- Rules are applied to every row (not just first occurrences) so each defect is counted once per source id.
insert into quarantine.records (load_id, entity, source_id, raw_id, reason, detail)
select :'load_id'::uuid, 'invoices', source_id, raw_id, reason, detail from (
    select raw_id, source_id, 'duplicate_source_id' as reason, 'repeat occurrence of the same id' as detail
      from s_invoices where rn > 1
    union all
    select raw_id, source_id, 'null_customer', 'customer_id is null'
      from s_invoices where customer_id is null
    union all
    select raw_id, source_id, 'orphan_customer', 'customer_id ' || customer_id || ' not found'
      from s_invoices s where customer_id is not null
       and not exists (select 1 from core.customers c where c.customer_id = s.customer_id)
    union all
    select raw_id, source_id, 'negative_total', 'total = ' || total
      from s_invoices where total is null or total < 0
    union all
    select raw_id, source_id, 'future_dated', 'issued_on ' || issued_on || ' is after ERP as-of date'
      from s_invoices where issued_on > (select erp_as_of from raw.load_runs where load_id = :'load_id'::uuid)
) q;

insert into core.invoices
select source_id, customer_id, issued_on, due_on, upper(currency), total, status, raw_id, :'load_id'::uuid
from s_invoices s
where rn = 1
  and not exists (select 1 from quarantine.records q
                   where q.load_id = :'load_id'::uuid and q.entity = 'invoices' and q.raw_id = s.raw_id);

-- ------------------------------------------------------------ invoice lines
create temp table s_lines on commit drop as
select raw_id, source_id,
       payload->>'invoice_id'          as invoice_id,
       payload->>'product'             as product,
       (payload->>'quantity')::int     as quantity,
       (payload->>'unit_price')::numeric as unit_price,
       (payload->>'amount')::numeric   as amount,
       row_number() over (partition by source_id order by page, position) as rn
from raw.records where load_id = :'load_id'::uuid and entity = 'invoice_lines';

insert into quarantine.records (load_id, entity, source_id, raw_id, reason, detail)
select :'load_id'::uuid, 'invoice_lines', source_id, raw_id, reason, detail from (
    select raw_id, source_id, 'duplicate_source_id' as reason, 'repeat occurrence of the same id' as detail
      from s_lines where rn > 1
    union all
    select raw_id, source_id,
           case when exists (select 1 from s_invoices x where x.source_id = s.invoice_id)
                then 'parent_quarantined' else 'orphan_invoice' end,
           'invoice ' || invoice_id || ' is not in core.invoices'
      from s_lines s where not exists (select 1 from core.invoices i where i.invoice_id = s.invoice_id)
) q;

insert into core.invoice_lines
select source_id, invoice_id, product, quantity, unit_price, amount, raw_id, :'load_id'::uuid
from s_lines s
where rn = 1
  and not exists (select 1 from quarantine.records q
                   where q.load_id = :'load_id'::uuid and q.entity = 'invoice_lines' and q.raw_id = s.raw_id);

-- ----------------------------------------------------------- ledger entries
create temp table s_ledger on commit drop as
select raw_id, source_id,
       payload->>'invoice_id'          as invoice_id,
       (payload->>'posted_on')::date   as posted_on,
       payload->>'account'             as account,
       payload->>'side'                as side,
       (payload->>'amount')::numeric   as amount,
       payload->>'kind'                as kind,
       row_number() over (partition by source_id order by page, position) as rn
from raw.records where load_id = :'load_id'::uuid and entity = 'ledger_entries';

insert into quarantine.records (load_id, entity, source_id, raw_id, reason, detail)
select :'load_id'::uuid, 'ledger_entries', source_id, raw_id, reason, detail from (
    select raw_id, source_id, 'duplicate_source_id' as reason, 'repeat occurrence of the same id' as detail
      from s_ledger where rn > 1
    union all
    select raw_id, source_id,
           case when exists (select 1 from s_invoices x where x.source_id = s.invoice_id)
                then 'parent_quarantined' else 'orphan_invoice' end,
           'invoice ' || invoice_id || ' is not in core.invoices'
      from s_ledger s where not exists (select 1 from core.invoices i where i.invoice_id = s.invoice_id)
) q;

insert into core.ledger_entries
select source_id, invoice_id, posted_on, account, side, amount, kind, raw_id, :'load_id'::uuid
from s_ledger s
where rn = 1
  and not exists (select 1 from quarantine.records q
                   where q.load_id = :'load_id'::uuid and q.entity = 'ledger_entries' and q.raw_id = s.raw_id);

-- ----------------------------------------------- reconciliation (kept, flagged)
-- Records that are structurally valid but whose numbers disagree across systems are NOT dropped.
-- They stay in core, get a row in dq.recon_breaks, and are excluded from "trusted" revenue.
create temp table inv_agg on commit drop as
select i.invoice_id, i.raw_id, i.total as header_total,
       (select sum(amount) from core.invoice_lines l where l.invoice_id = i.invoice_id) as lines_total,
       (select sum(amount) from core.ledger_entries g
         where g.invoice_id = i.invoice_id and g.kind = 'invoice' and g.account = '4000-REVENUE' and g.side = 'credit') as ledger_revenue,
       (select sum(amount) from core.ledger_entries g
         where g.invoice_id = i.invoice_id and g.kind = 'invoice' and g.side = 'debit')  as ledger_debit,
       (select sum(amount) from core.ledger_entries g
         where g.invoice_id = i.invoice_id and g.kind = 'invoice' and g.side = 'credit') as ledger_credit
from core.invoices i;

insert into dq.recon_breaks (load_id, invoice_id, break_type, header_total, lines_total, ledger_revenue, ledger_debit, ledger_credit, diff, raw_id)
select :'load_id'::uuid, invoice_id, 'header_vs_lines', header_total, lines_total, ledger_revenue, ledger_debit, ledger_credit,
       header_total - coalesce(lines_total, 0), raw_id
from inv_agg where abs(header_total - coalesce(lines_total, 0)) > 0.005
union all
select :'load_id'::uuid, invoice_id, 'ledger_unbalanced', header_total, lines_total, ledger_revenue, ledger_debit, ledger_credit,
       coalesce(ledger_debit, 0) - coalesce(ledger_credit, 0), raw_id
from inv_agg where abs(coalesce(ledger_debit, 0) - coalesce(ledger_credit, 0)) > 0.005
union all
select :'load_id'::uuid, invoice_id, 'header_vs_ledger', header_total, lines_total, ledger_revenue, ledger_debit, ledger_credit,
       header_total - coalesce(ledger_revenue, 0), raw_id
from inv_agg where abs(header_total - coalesce(ledger_revenue, 0)) > 0.005;

update raw.load_runs set status = 'transformed' where load_id = :'load_id'::uuid;
commit;
