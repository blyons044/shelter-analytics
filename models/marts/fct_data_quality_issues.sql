{{
    config(
        materialized='table'
    )
}}

{#
    Rows excluded from the reporting models, and why.

    Quarantining rather than deleting means the volume of excluded records is
    itself reportable. A quiet filter hides a feed that is degrading; a table
    with a row count makes it a number someone can watch.
#}

with stays as (

    select * from {{ ref('int_shelter_stays') }}

),

invalid_duration as (

    select
        stay_key,
        animal_id,
        'outcome_before_intake'                                  as issue_type,
        'Outcome timestamp precedes intake timestamp. Indicates a missing '
        || 'record in the animal''s history shifting the intake-to-outcome '
        || 'pairing, or a source data entry error.'              as issue_description,
        intake_at,
        outcome_at
    from stays
    where has_invalid_duration

),

unknown_outcome as (

    select
        stay_key,
        animal_id,
        'unrecognised_outcome_type'                              as issue_type,
        'Outcome type is not in the known live or non-live sets, so the stay '
        || 'is absent from live release rate calculations.'       as issue_description,
        intake_at,
        outcome_at
    from stays
    where outcome_class = 'Unknown'

),

missing_age as (

    select
        stay_key,
        animal_id,
        'unusable_age_at_intake'                                 as issue_type,
        'Age on intake was absent, non-numeric, or negative, so no age band '
        || 'could be assigned.'                                   as issue_description,
        intake_at,
        outcome_at
    from stays
    where age_at_intake_days is null

),

orphan_outcomes as (

    {#
        Outcomes with no matching intake.

        int_shelter_stays builds outward from intakes, so an outcome whose
        stay_key has no partner never reaches the fact table at all. That is
        the correct behaviour (a stay with no beginning has no duration and no
        intake attributes) but it must not be silent: these rows are real
        outcomes, and dropping them without a count would understate outcome
        volume and shift the live release rate by whatever those animals did.

        The live feed carries them for animals whose intake predates the
        published range, and for gaps in the outcome history.
    #}

    select
        o.stay_key,
        o.animal_id,
        'outcome_without_intake'                                 as issue_type,
        'Outcome has no matching intake at the same stay sequence, so the '
        || 'stay is absent from fct_shelter_stays entirely. Usually an animal '
        || 'whose intake predates the published data range.'      as issue_description,
        cast(null as timestamp)                                  as intake_at,
        o.outcome_at
    from {{ ref('stg_shelter__outcomes') }} o
    left join {{ ref('stg_shelter__intakes') }} i
        on o.stay_key = i.stay_key
    where i.stay_key is null

)

select * from invalid_duration
union all
select * from unknown_outcome
union all
select * from missing_age
union all
select * from orphan_outcomes
