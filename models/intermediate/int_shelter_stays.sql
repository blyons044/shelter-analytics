{#
    One row per shelter stay: an intake, and the outcome that closed it.

    Pairing rule
    ------------
    Neither feed carries a stay identifier, and animals return. Intakes and
    outcomes are therefore matched on (animal_id, stay_sequence): the nth
    outcome closes the nth intake. A left join keeps animals currently in care,
    which have an intake and no outcome yet.

    Where the pairing can be wrong
    ------------------------------
    If the feed is missing an outcome in the middle of an animal's history,
    every later outcome for that animal shifts up one position and pairs with
    the wrong intake. This is detectable rather than silent: a mispaired row
    produces an outcome dated before its intake, which is flagged below and
    tested in the mart. Roughly 0.1% of stays fail that check on the live feed.
    They are excluded from duration metrics and counted in a data-quality model
    rather than dropped quietly.
#}

with intakes as (

    select * from {{ ref('stg_shelter__intakes') }}

),

outcomes as (

    select * from {{ ref('stg_shelter__outcomes') }}

),

paired as (

    select
        i.stay_key,
        i.animal_id,
        i.stay_sequence,

        i.intake_at,
        o.outcome_at,

        i.intake_type,
        i.intake_condition,
        o.outcome_type,
        o.outcome_subtype,

        -- Attributes can change between intake and outcome (an animal is
        -- neutered while in care). Intake values describe the animal as it
        -- arrived, which is what most questions are about.
        coalesce(i.animal_type, o.animal_type)                   as animal_type,
        coalesce(i.animal_name, o.animal_name)                   as animal_name,
        coalesce(i.breed, o.breed)                               as breed,
        coalesce(i.color, o.color)                               as color,
        i.sex_upon_intake,
        o.sex_upon_outcome,
        i.found_location,
        o.date_of_birth,

        i.age_at_intake_days,
        o.age_at_outcome_days

    from intakes i
    left join outcomes o
        on  i.animal_id = o.animal_id
        and i.stay_sequence = o.stay_sequence

),

classified as (

    select
        *,

        outcome_at is null                                       as is_open_stay,

        -- Guard before arithmetic: a negative duration means the pairing or
        -- the source record is wrong, and the number would be worse than
        -- missing.
        case
            when outcome_at is null then false
            when outcome_at < intake_at then true
            else false
        end                                                      as has_invalid_duration,

        case
            when outcome_at is null then null
            when outcome_at < intake_at then null
            else date_diff('day', intake_at, outcome_at)
        end                                                      as length_of_stay_days,

        {{ classify_outcome('outcome_type') }}                    as outcome_class,

        -- Owner-requested euthanasia is excluded from the adjusted live
        -- release rate. See the README on metric definitions.
        intake_type = 'Euthanasia Request'                       as is_owner_requested_euthanasia

    from paired

)

select * from classified
