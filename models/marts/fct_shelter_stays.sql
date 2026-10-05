{{
    config(
        materialized='table'
    )
}}

{#
    Grain: one row per shelter stay.

    Stays whose outcome predates their intake are excluded here and surfaced in
    fct_data_quality_issues instead. They are a pairing or source defect, and
    leaving them in would corrupt every duration metric downstream.
#}

with stays as (

    select * from {{ ref('int_shelter_stays') }}

)

select
    stay_key,
    animal_id,
    stay_sequence,
    stay_sequence > 1                                            as is_return_stay,

    intake_at,
    cast(intake_at as date)                                      as intake_date,
    date_trunc('month', intake_at)                               as intake_month,

    outcome_at,
    cast(outcome_at as date)                                     as outcome_date,
    date_trunc('month', outcome_at)                              as outcome_month,

    is_open_stay,
    length_of_stay_days,

    intake_type,
    intake_condition,
    outcome_type,
    outcome_subtype,
    outcome_class,
    is_owner_requested_euthanasia,

    animal_type,
    animal_name,
    animal_name is not null                                      as was_named,
    breed,
    color,
    sex_upon_intake,
    sex_upon_outcome,
    found_location,

    age_at_intake_days,
    case
        when age_at_intake_days is null then null
        when age_at_intake_days < 180 then 'Under 6 months'
        when age_at_intake_days < 365 then '6 to 12 months'
        when age_at_intake_days < 1095 then '1 to 3 years'
        when age_at_intake_days < 2555 then '3 to 7 years'
        else '7 years and over'
    end                                                          as age_band_at_intake

from stays
where not has_invalid_duration
