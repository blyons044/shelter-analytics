{{
    config(
        materialized='view'
    )
}}

{#
    Typing and standardisation for the intake feed.

    Three things happen here and nowhere else:
      1. the free-text age string becomes a number of days
      2. categorical fields are trimmed and title-cased, because the feed
         carries the same value in several casings
      3. exact duplicate rows are collapsed

    A stay sequence number is assigned per animal so that intakes can later be
    paired with the outcome that actually ended that stay. Animals return to
    the shelter, so animal_id on its own is not a stay key.
#}

with source as (

    select * from {{ source('raw', 'intakes') }}

),

deduplicated as (

    -- The portal repeats rows on occasion. An intake is identified by the
    -- animal and the timestamp; everything else is attributes of that event.
    select distinct
        animal_id,
        datetime,
        name,
        found_location,
        intake_type,
        intake_condition,
        animal_type,
        sex_upon_intake,
        age_upon_intake,
        breed,
        color
    from source
    where animal_id is not null
      and datetime is not null

),

cleaned as (

    select
        animal_id,
        cast(datetime as timestamp)                              as intake_at,
        nullif(trim(name), '')                                   as animal_name,
        nullif(trim(found_location), '')                         as found_location,
        {{ clean_label('intake_type') }}                          as intake_type,
        {{ clean_label('intake_condition') }}                     as intake_condition,
        {{ clean_label('animal_type') }}                          as animal_type,
        {{ clean_label('sex_upon_intake') }}                      as sex_upon_intake,
        {{ age_string_to_days('age_upon_intake') }}               as age_at_intake_days,
        nullif(trim(breed), '')                                  as breed,
        nullif(trim(color), '')                                  as color
    from deduplicated

),

sequenced as (

    select
        *,
        row_number() over (
            partition by animal_id
            order by intake_at
        ) as stay_sequence
    from cleaned

)

select
    {{ surrogate_key(['animal_id', 'stay_sequence']) }} as stay_key,
    animal_id,
    stay_sequence,
    intake_at,
    animal_name,
    found_location,
    intake_type,
    intake_condition,
    animal_type,
    sex_upon_intake,
    age_at_intake_days,
    breed,
    color
from sequenced
