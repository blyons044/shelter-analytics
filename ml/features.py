"""
Feature and label construction for the long-stay model.

Two rules govern everything in this file.

1. A feature must be knowable at the moment of intake. The model is meant to be
   run when an animal arrives, so anything recorded later, even if it sits on
   the same row, is leakage. That rules out outcome fields, sex at outcome, and
   the animal's name (the published name can be edited after intake, and
   animals that get adopted tend to get named).

   Sex is left out too, for a different reason: on Austin's data it added
   nothing once spay/neuter status was in (AUC 0.726 with it, 0.725 without),
   and a feature that adds nothing is one more thing to defend. Spay/neuter
   status stays. Its "Unknown" value turned out to mark animals staff could not
   examine on arrival, mostly community cats in the shelter-neuter-return
   program and very young litters sent to partners. That is information
   genuinely available on day one. See the README.

2. A label only exists once enough time has passed to know it. A stay is "long"
   at day 30, so an animal that arrived 12 days ago has no label yet, whether or
   not it has already left. Keeping the ones that left quickly and dropping the
   ones still in care would bias every recent label toward "short".
"""

from __future__ import annotations

from dataclasses import dataclass

import duckdb
import numpy as np
import pandas as pd

LONG_STAY_DAYS = 30

# Dogs and cats are the population where length of stay is a lever staff can
# pull on (foster outreach, featured listings, rescue pulls). Wildlife and
# owner-requested euthanasia follow different processes entirely.
ANIMAL_TYPES = ("Dog", "Cat")
EXCLUDED_INTAKE_TYPES = ("Euthanasia Request", "Wildlife")

# The occupancy feature counts animals in care, which the feed can only do for
# animals whose intake it contains. The first months of the feed undercount, so
# they are not used for training.
CENSUS_WARMUP_DAYS = 180

# An open stay older than this is far more likely to be a missing outcome in
# the feed than an animal genuinely in care for a year. Labelling those as long
# stays would teach the model the feed's gaps, so they are left out and counted.
MAX_PLAUSIBLE_OPEN_DAYS = 365

CATEGORICAL_FEATURES = [
    "animal_type",
    "intake_type",
    "intake_condition",
    "reproductive_status",
    "is_mix",
    "primary_breed",
    "primary_color",
    "jurisdiction",
    "intake_month",
    "intake_weekday",
]

NUMERIC_FEATURES = [
    "age_at_intake_days",
    "stay_sequence",
    "census_same_type",
]

FEATURES = CATEGORICAL_FEATURES + NUMERIC_FEATURES


@dataclass(frozen=True)
class Frames:
    as_of: pd.Timestamp
    label_cutoff: pd.Timestamp
    labelled: pd.DataFrame
    unlabelled_open: pd.DataFrame
    excluded_implausible_open: int


_STAYS_SQL = """
with stays as (

    select *
    from main_marts.fct_shelter_stays

),

-- Animals in care at the start of each day, by animal type. Built from
-- events rather than a range join: +1 on the intake day, -1 on the outcome
-- day, then a running total. Everything in it is known on the morning of the
-- intake, so it is a legitimate intake-time feature.
events as (

    select animal_type, cast(intake_at as date) as event_date, 1 as delta
    from stays
    union all
    select animal_type, cast(outcome_at as date) as event_date, -1 as delta
    from stays
    where outcome_at is not null

),

daily as (

    select animal_type, event_date, sum(delta) as net_change
    from events
    group by 1, 2

),

census as (

    select
        animal_type,
        event_date,
        -- Running total up to the previous day: the census on the morning of
        -- event_date, before that day's arrivals and departures.
        coalesce(
            sum(net_change) over (
                partition by animal_type
                order by event_date
                rows between unbounded preceding and 1 preceding
            ),
            0
        ) as census_morning
    from daily

)

select
    s.stay_key,
    s.animal_id,
    s.stay_sequence,
    s.intake_at,
    s.outcome_at,
    s.is_open_stay,
    s.length_of_stay_days,
    s.animal_type,
    s.intake_type,
    s.intake_condition,
    s.sex_upon_intake,
    s.outcome_subtype,
    s.breed,
    s.color,
    s.found_location,
    s.age_at_intake_days,
    c.census_morning as census_same_type
from stays s
left join census c
    on  c.animal_type = s.animal_type
    and c.event_date = cast(s.intake_at as date)
-- A fixed row order makes runs reproducible: the model's internal validation
-- split is seeded, but a seed only helps if the rows arrive in the same order.
order by s.stay_key
"""


def _reproductive_status(value: object) -> str:
    text = str(value or "")
    if "Intact" in text:
        return "Intact"
    if "Neutered" in text or "Spayed" in text:
        return "Altered"
    return "Unknown"


def _primary(value: object) -> str:
    """First component of a slash-separated breed or colour, without 'Mix'."""
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return "Unknown"
    first = str(value).split("/")[0].replace(" Mix", "").strip()
    return first or "Unknown"


def _jurisdiction(value: object) -> str:
    """City from 'Address in Austin (TX)' or 'Austin (TX)' style strings."""
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return "Unknown"
    text = str(value)
    if "Outside Jurisdiction" in text:
        return "Outside Jurisdiction"
    if " in " in text:
        text = text.rsplit(" in ", 1)[1]
    city = text.split("(")[0].strip()
    return city or "Unknown"


def derive(df: pd.DataFrame) -> pd.DataFrame:
    """Turn warehouse columns into model inputs. Stateless: no fitted values."""
    out = pd.DataFrame(index=df.index)
    out["animal_type"] = df["animal_type"].fillna("Unknown")
    out["intake_type"] = df["intake_type"].fillna("Unknown")
    out["intake_condition"] = df["intake_condition"].fillna("Unknown")
    out["reproductive_status"] = df["sex_upon_intake"].map(_reproductive_status)
    breed = df["breed"].fillna("")
    out["is_mix"] = np.where(breed.str.contains("Mix|/", regex=True), "Mix", "Single")
    out["primary_breed"] = df["breed"].map(_primary)
    out["primary_color"] = df["color"].map(_primary)
    out["jurisdiction"] = df["found_location"].map(_jurisdiction)
    out["intake_month"] = df["intake_at"].dt.month.astype(str)
    out["intake_weekday"] = df["intake_at"].dt.day_name()
    out["age_at_intake_days"] = pd.to_numeric(df["age_at_intake_days"], errors="coerce").astype(float)
    out["stay_sequence"] = df["stay_sequence"].clip(upper=3).astype(float)
    out["census_same_type"] = pd.to_numeric(df["census_same_type"], errors="coerce").astype(float)
    return out


def load(con: duckdb.DuckDBPyConnection) -> Frames:
    raw = con.execute(_STAYS_SQL).df()
    raw["intake_at"] = pd.to_datetime(raw["intake_at"])
    raw["outcome_at"] = pd.to_datetime(raw["outcome_at"])

    # "Now" is the latest event in the data, not the wall clock. That keeps runs
    # reproducible and lets the sample data behave like a real snapshot.
    as_of = max(raw["intake_at"].max(), raw["outcome_at"].max())
    label_cutoff = (as_of - pd.Timedelta(days=LONG_STAY_DAYS)).normalize()
    feed_start = raw["intake_at"].min()

    eligible = raw[
        raw["animal_type"].isin(ANIMAL_TYPES)
        & ~raw["intake_type"].isin(EXCLUDED_INTAKE_TYPES)
    ].copy()

    # Label maturity is decided by intake date alone, never by whether the stay
    # has closed. See rule 2 in the module docstring.
    mature = eligible["intake_at"] < label_cutoff
    warmed_up = eligible["intake_at"] >= feed_start + pd.Timedelta(days=CENSUS_WARMUP_DAYS)

    implausible_open = eligible["is_open_stay"] & (
        eligible["intake_at"] < as_of - pd.Timedelta(days=MAX_PLAUSIBLE_OPEN_DAYS)
    )

    labelled = eligible[mature & warmed_up & ~implausible_open].copy()
    # A stay still open after 30 days is long by definition; its eventual
    # length does not matter for this label.
    labelled["is_long_stay"] = (
        labelled["is_open_stay"] | (labelled["length_of_stay_days"] >= LONG_STAY_DAYS)
    ).astype(int)

    unlabelled_open = eligible[~mature & eligible["is_open_stay"]].copy()

    return Frames(
        as_of=as_of,
        label_cutoff=label_cutoff,
        labelled=labelled.reset_index(drop=True),
        unlabelled_open=unlabelled_open.reset_index(drop=True),
        excluded_implausible_open=int(implausible_open.sum()),
    )
