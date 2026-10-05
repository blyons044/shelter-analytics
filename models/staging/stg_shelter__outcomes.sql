{{
    config(
        materialized='view'
    )
}}

{#
    Typing and standardisation for the outcome feed.

    Mirrors the intake staging model, including the per-animal sequence number.
    The nth outcome for an animal closes the nth intake, which is the only
    reliable way to pair the two feeds: neither carries a stay identifier.
#}

with source as (

    select * from {{ source('raw', 'outcomes') }}

),

deduplicated as (

    select distinct
        animal_id,
        datetime,
        name,
        date_of_birth,
        outcome_type,
        outcome_subtype,
        animal_type,
        sex_upon_outcome,
        age_upon_outcome,
        breed,
        color
    from source
    where animal_id is not null
      and datetime is not null

),

cleaned as (

    select
        animal_id,
        cast(datetime as timestamp)                              as outcome_at,
        try_cast(date_of_birth as timestamp)                     as date_of_birth,
        nullif(trim(name), '')                                   as animal_name,
        {{ clean_label('outcome_type') }}                         as outcome_type,
        {{ clean_label('outcome_subtype') }}                      as outcome_subtype,
        {{ clean_label('animal_type') }}                          as animal_type,
        {{ clean_label('sex_upon_outcome') }}                     as sex_upon_outcome,
        {{ age_string_to_days('age_upon_outcome') }}              as age_at_outcome_days,
        nullif(trim(breed), '')                                  as breed,
        nullif(trim(color), '')                                  as color
    from deduplicated

),

sequenced as (

    select
        *,
        row_number() over (
            partition by animal_id
            order by outcome_at
        ) as stay_sequence
    from cleaned

)

select
    {{ surrogate_key(['animal_id', 'stay_sequence']) }} as stay_key,
    animal_id,
    stay_sequence,
    outcome_at,
    date_of_birth,
    animal_name,
    outcome_type,
    outcome_subtype,
    animal_type,
    sex_upon_outcome,
    age_at_outcome_days,
    breed,
    color
from sequenced
