{{
    config(
        materialized='table'
    )
}}

{#
    Grain: one row per animal.

    Attributes are taken from the animal's most recent stay, since breed and
    colour are occasionally corrected between visits and the latest record is
    the best available. Stay counts and first and last seen dates summarise the
    animal's whole history.
#}

with stays as (

    select * from {{ ref('fct_shelter_stays') }}

),

latest as (

    select
        animal_id,
        animal_name,
        animal_type,
        breed,
        color,
        sex_upon_outcome,
        row_number() over (
            partition by animal_id
            order by intake_at desc
        ) as recency
    from stays

),

history as (

    select
        animal_id,
        count(*)                                                 as stay_count,
        min(intake_at)                                           as first_intake_at,
        max(intake_at)                                           as last_intake_at,
        max(outcome_at)                                          as last_outcome_at,
        bool_or(is_open_stay)                                     as is_currently_in_care,
        sum(length_of_stay_days)                                 as total_days_in_care
    from stays
    group by 1

)

select
    h.animal_id,
    l.animal_name,
    l.animal_type,
    l.breed,
    l.color,
    l.sex_upon_outcome                                           as sex_latest,

    h.stay_count,
    h.stay_count > 1                                             as has_returned,
    h.first_intake_at,
    h.last_intake_at,
    h.last_outcome_at,
    h.is_currently_in_care,
    h.total_days_in_care

from history h
left join latest l
    on h.animal_id = l.animal_id
   and l.recency = 1
