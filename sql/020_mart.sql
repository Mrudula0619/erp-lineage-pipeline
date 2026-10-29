-- Reporting layer. Only built when no critical data quality check failed (see pipeline/dq.py).
-- "Trusted" = the invoice reconciles across header, lines and ledger.
\set ON_ERROR_STOP on
begin;

create or replace view mart.invoice_facts as
select i.invoice_id,
       i.customer_id,
       c.name                                   as customer_name,
       c.region,
       date_trunc('month', i.issued_on)::date   as month,
       i.issued_on,
       i.total,
       i.status,
       i.raw_id,
       not exists (select 1 from dq.recon_breaks b
                    where b.invoice_id = i.invoice_id and b.load_id = i.load_id) as is_trusted
from core.invoices i
join core.customers c using (customer_id);

drop table if exists mart.monthly_revenue;
create table mart.monthly_revenue as
select month,
       sum(total)     filter (where is_trusted)     as trusted_revenue,
       count(*)       filter (where is_trusted)     as trusted_invoices,
       coalesce(sum(total) filter (where not is_trusted), 0) as unreconciled_revenue,
       count(*)       filter (where not is_trusted) as unreconciled_invoices
from mart.invoice_facts
group by month
order by month;

update raw.load_runs set status = 'published' where load_id = :'load_id'::uuid;
commit;
