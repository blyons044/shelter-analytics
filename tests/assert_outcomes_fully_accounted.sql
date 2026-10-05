-- Every staged outcome must land somewhere: either paired into a stay in
-- fct_shelter_stays, or quarantined in fct_data_quality_issues with a reason.
--
-- This is the test that would have caught the orphan outcomes. Without it, an
-- outcome with no matching intake disappears between the staging layer and the
-- fact table and nothing in the project notices.

with staged as (
    select count(*) as n from {{ ref('stg_shelter__outcomes') }}
),

accounted as (
    select
        (select count(*) from {{ ref('fct_shelter_stays') }} where not is_open_stay)
        + (select count(distinct stay_key) from {{ ref('fct_data_quality_issues') }}
           where issue_type in ('outcome_without_intake', 'outcome_before_intake'))
        as n
)

select staged.n as staged_outcomes, accounted.n as accounted_outcomes
from staged, accounted
where staged.n != accounted.n
