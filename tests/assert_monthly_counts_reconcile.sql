-- The monthly reporting table must agree with the fact table it is built from.
-- A mismatch means a group-by or a filter in the mart dropped stays, which is
-- the failure mode that is hardest to notice by eye.

with mart_total as (
    select sum(outcomes_total) as n from {{ ref('mart_monthly_outcomes') }}
),

fact_total as (
    select count(*) as n
    from {{ ref('fct_shelter_stays') }}
    where not is_open_stay
)

select
    mart_total.n as mart_rows,
    fact_total.n as fact_rows
from mart_total, fact_total
where mart_total.n != fact_total.n
