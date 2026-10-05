# Shelter Analytics

A dbt project modelling Austin Animal Center intake and outcome records into a
tested, documented warehouse, with a scheduled pipeline that rebuilds it daily.

The source is two public feeds that do not join cleanly. Everything interesting
in this project is about closing that gap honestly.

```
data.austintexas.gov          ingest/extract.py        dbt
  intakes  (wter-evkm)  ──▶   raw.intakes       ──▶   staging ──▶ intermediate ──▶ marts
  outcomes (9t4d-g238)  ──▶   raw.outcomes                           │
                                                                     └──▶ fct_data_quality_issues
```

**Stack:** dbt Core, DuckDB, Python, GitHub Actions. No package dependencies.
Clone it, install the requirements, and it runs.

---

## Why this dataset

Two feeds, one row per event, no stay identifier in either, and animals that
come back. Working out which outcome closed which intake is the whole problem,
and it is the kind of problem that does not appear in tutorial datasets: the
join is ambiguous, the ambiguity is invisible if you do not look for it, and the
wrong answer still produces a dashboard that renders.

## Running it

```bash
pip install -r requirements.txt
export DBT_PROFILES_DIR=$(pwd)

python -m ingest.extract --sample 8000   # offline, synthetic, same schema
# or
python -m ingest.extract                 # live pull from the city's API

dbt build          # runs models and tests together
dbt docs generate && dbt docs serve
```

`--sample` generates data that reproduces the parts of the real feed the models
have to survive: repeat stays, animals still in care, free-text ages including
the literal string `NULL`, inconsistent casing, duplicate rows, and a small
number of outcomes dated before their intake.

---

## The modelling problem

Neither feed carries a stay identifier. An animal that is adopted, returned six
months later, and adopted again appears as three intakes and three outcomes
sharing one `animal_id`.

Joining on `animal_id` alone produces a cross product: three intakes times three
outcomes is nine rows where there should be three, and every duration metric
built on top is wrong.

**The pairing rule used here** is to number each animal's intakes and outcomes
by timestamp and join on `(animal_id, stay_sequence)`. The nth outcome closes
the nth intake.

**Where that rule breaks.** If the feed is missing an outcome in the middle of
an animal's history, every later outcome shifts up a position and pairs with the
wrong intake. This is not hypothetical, and it is the reason the project does
not simply trust the join:

- A mispaired row usually surfaces as an outcome dated before its intake.
- Those rows are flagged in `int_shelter_stays`, excluded from
  `fct_shelter_stays`, and written to `fct_data_quality_issues` with a reason.
- `tests/assert_no_negative_stay_duration.sql` then proves the exclusion held,
  rather than assuming it.

Quarantining rather than deleting matters. A silent filter hides a feed that is
degrading. A table with a row count makes it something a person can watch.

---

## Metric definitions

The point of writing these down is that each one encodes a choice someone could
reasonably have made differently.

**Live release rate.** Live outcomes over all classified outcomes. Live covers
adoption, return to owner, Rto-Adopt, and transfer, following the Shelter
Animals Count basic matrix.

- `Missing` counts as non-live. An animal unaccounted for is not a live
  release, and classifying it as unknown would quietly inflate the rate.
- Unrecognised outcome types classify as `Unknown` and are excluded from both
  numerator and denominator, so a new outcome type appearing in the feed shows
  up in `fct_data_quality_issues` instead of silently moving the metric. The
  `accepted_values` test on `outcome_type` is what surfaces it.
- A grouping with no classified outcomes returns null, not zero. Zero percent
  and no data are different claims.

**Adjusted live release rate.** The same calculation with animals surrendered
specifically for euthanasia removed from both numerator and denominator.

Shelters are commonly measured on the unadjusted figure, which penalises those
that accept owner-requested euthanasia rather than turning people away. Both are
published here because the gap between them is itself a finding: in the sample
data it averages about 4.8 points, and a shelter's policy on these intakes moves
that gap more than its actual outcomes do.

**Length of stay.** Whole days between intake and outcome, for closed stays
only. Median is the headline measure, with mean and 90th percentile alongside
it. The distribution is heavily right skewed by long-term medical and
behavioural cases, so the mean sits well above the typical animal's experience.
Carrying all three keeps the skew visible rather than averaging it away.

**Month attribution.** `mart_monthly_outcomes` is keyed on outcome month, not
intake month. A stay running March to June belongs to June: the question is what
happened to the animals released that month, not what became of the animals who
arrived in it. Intake volume by arrival month is a different question and is
answered from `fct_shelter_stays`.

**Open stays are excluded entirely** from outcome metrics. Including them would
drag the live release rate down for recent months purely because those animals
have not left yet, which reads as a decline that is not there.

### A caveat the models cannot fix

Recent months are right censored. Animals taken in last week are mostly still in
care, so the stays that close quickly are over-represented in the most recent
month and the median length of stay reads low. `outcomes_total` is carried on
every row so a consumer can filter thin months rather than being misled by them.
Treat the trailing two months as provisional.

---

## Models

| Model | Grain | Notes |
| --- | --- | --- |
| `stg_shelter__intakes` | One row per intake | Typing, dedupe, stay sequencing |
| `stg_shelter__outcomes` | One row per outcome | Typing, dedupe, stay sequencing |
| `int_shelter_stays` | One row per stay | The pairing, duration guard, outcome classification |
| `fct_shelter_stays` | One row per stay | Reporting grain, invalid rows excluded |
| `dim_animal` | One row per animal | Latest attributes, full stay history |
| `mart_monthly_outcomes` | Month by animal type | Live release rate, length of stay |
| `fct_data_quality_issues` | One row per issue | What was excluded, and why |

## Testing

54 tests run on every build, as part of `dbt build` rather than a separate step,
so a model that breaks its own contract does not get published first and tested
afterwards.

Schema tests cover uniqueness and nullability on every key, accepted values on
every categorical that feeds a metric, referential integrity between the fact
and the dimension, and numeric ranges on measures.

Five custom tests cover the things schema tests cannot:

- `assert_no_negative_stay_duration` proves the temporal filter held.
- `assert_open_stays_have_no_outcome` catches the flag and the data disagreeing.
- `assert_monthly_counts_reconcile` proves the mart did not drop stays in its
  group-by, which is the failure mode hardest to spot by eye.
- `assert_live_rate_matches_components` proves the stored rate still equals what
  its own component counts imply, guarding against the macro and the counts
  drifting apart.
- `assert_outcomes_fully_accounted` proves every staged outcome either paired
  into a stay or was quarantined with a reason. This is the test that caught the
  orphan outcomes described below.

Two tests are set to `warn` rather than `error`: the accepted-value lists on
`intake_type` and `outcome_type`. A new category appearing in a public feed is
news, not a reason to fail the build and leave yesterday's numbers in place.

## Orchestration

`.github/workflows/pipeline.yml` runs the extract, the build, and the tests
daily, publishes the dbt docs to Pages, and writes a pass/fail table to the run
summary.

Pull requests run against sample data rather than the live API, so a fork cannot
trigger a full extract against the city's servers. The schema is identical
either way, so the models are tested just as hard.

The schedule is set to 09:12 UTC rather than on the hour. Scheduled jobs cluster
at :00, GitHub queues them, and a two minute run becomes a twenty minute wait.

## Design notes

**Full refresh, not incremental.** Both source datasets are snapshots that get
revised in place when staff correct a record after the fact. An incremental
merge on `animal_id` would keep stale rows for any animal with repeat stays. At
this volume the rebuild costs seconds, and correctness is worth more than the
seconds.

**Everything lands as text.** The API returns strings, and roughly 2% of age
values are unparseable. Casting at ingest would either drop those rows or fail
the load. Staging handles them where the rules are visible, documented, and
testable.

**Bulk load, not row-by-row inserts.** Pages are streamed to newline-delimited
JSON and read by DuckDB in one scan. The first version of this used
`executemany`, which binds each row individually: at roughly 190k records per
table that turned a seconds-long load into a multi-minute one. Streaming also
keeps memory flat, since no page is held once it has been written.

**Missing columns are substituted, not assumed.** Socrata omits keys whose
value is null rather than emitting a null, so a column that is empty across the
whole feed does not appear in the scanned file at all. The loader checks which
columns were discovered and fills the rest with null, and verifies the loaded
row count matches what was staged.

**No package dependencies.** `surrogate_key` and the `accepted_range` generic
test are implemented in `macros/`. dbt_utils would do both, but a portfolio
project that clones and runs with nothing but a requirements file is worth more
than the two functions saved.

**Negative ages become null, not absolute values.** A negative age is evidence
the record is wrong, not evidence of its magnitude.

**Outcomes with no intake.** `int_shelter_stays` builds outward from intakes, so
an outcome whose `stay_key` has no partner never reaches the fact table. That is
correct, since a stay with no beginning has no duration and no intake
attributes, but it must not be silent: these are real outcomes, and dropping
them without a count would understate outcome volume and move the live release
rate by whatever those animals did. They are written to
`fct_data_quality_issues` as `outcome_without_intake`, and
`assert_outcomes_fully_accounted` fails the build if any outcome goes missing
without landing in one place or the other.

This one was not in the original design. It surfaced while checking what the
left join was quietly discarding, which is the reason the reconciliation test
now exists.

---

## Data source

Intake and outcome records published by the City of Austin under the
[Public Domain Dedication and License](https://data.austintexas.gov).
This project is not affiliated with the City of Austin or Austin Animal Center.
