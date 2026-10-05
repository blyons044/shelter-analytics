{{
    config(
        materialized='table'
    )
}}

{#
    Grain: one row per outcome month per animal type.

    Keyed on outcome month, not intake month. A stay that begins in March and
    ends in June belongs to June here: the question this table answers is what
    happened to the animals released in a given month, not what became of the
    animals who arrived in it. Intake volume by arrival month is a different
    question and is answered from fct_shelter_stays.

    Open stays are excluded entirely. Including them would push the live
    release rate down for recent months purely because those animals have not
    left yet, which reads as a decline that is not there.
#}

with stays as (

    select *
    from {{ ref('fct_shelter_stays') }}
    where not is_open_stay

),

monthly as (

    select
        outcome_month,
        animal_type,

        count(*)                                                 as outcomes_total,
        count(*) filter (where outcome_class = 'Live')           as outcomes_live,
        count(*) filter (where outcome_class = 'Non-Live')       as outcomes_non_live,
        count(*) filter (where outcome_class = 'Unknown')        as outcomes_unclassified,

        count(*) filter (where outcome_type = 'Adoption')        as outcomes_adoption,
        count(*) filter (where outcome_type = 'Transfer')        as outcomes_transfer,
        count(*) filter (where outcome_type = 'Return To Owner') as outcomes_return_to_owner,
        count(*) filter (where outcome_type = 'Euthanasia')      as outcomes_euthanasia,

        count(*) filter (where is_return_stay)                   as outcomes_return_stays,

        {{ live_release_rate('outcome_class') }}                  as live_release_rate_pct,
        {{ live_release_rate('outcome_class', 'is_owner_requested_euthanasia') }}
                                                                 as live_release_rate_adjusted_pct,

        round(avg(length_of_stay_days), 1)                       as avg_length_of_stay_days,
        median(length_of_stay_days)                              as median_length_of_stay_days,
        quantile_cont(length_of_stay_days, 0.90)                 as p90_length_of_stay_days

    from stays
    group by 1, 2

)

select
    outcome_month,
    animal_type,
    outcomes_total,
    outcomes_live,
    outcomes_non_live,
    outcomes_unclassified,
    outcomes_adoption,
    outcomes_transfer,
    outcomes_return_to_owner,
    outcomes_euthanasia,
    outcomes_return_stays,
    live_release_rate_pct,
    live_release_rate_adjusted_pct,

    -- Median is the headline duration measure. Length of stay is heavily right
    -- skewed by a small number of long-term medical and behavioural cases, so
    -- the mean sits well above the typical animal's experience. Both are kept
    -- so the skew itself stays visible.
    avg_length_of_stay_days,
    median_length_of_stay_days,
    p90_length_of_stay_days

from monthly
order by outcome_month, animal_type
