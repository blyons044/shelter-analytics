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

fct_shelter_stays ──▶ ml/train.py ──▶ ml.stay_scores, ml.monthly_performance, ...
```

**Stack:** dbt Core, DuckDB, Python, scikit-learn, GitHub Actions. No dbt package
dependencies.
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

dbt build                  # runs models and tests together
python -m ml.train         # trains, backtests, and scores the long-stay model
dbt build --selector ml    # tests the model's outputs
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

## Predicting long stays

The warehouse answers what happened. The model in `ml/` asks something more
useful to a shelter: **when a dog or cat arrives, how likely is it to still be in
care 30 days later?**

The usual suspects for a long stay are age, condition on arrival, and how full
the shelter is. Whether those hold in Austin's data is what the importance table
is for. If staff can see the risk on day one, foster outreach and rescue conversations can start while the
animal is still healthy and adoptable, instead of after it has spent a month in
a kennel. The output is a ranked list of animals currently in care for under 30
days, in `ml.stay_scores`, with a probability and a High, Elevated, or Typical
band.

### Population and label

Dogs and cats only, excluding owner-requested euthanasia and wildlife, which
follow different processes. A stay is long if it reaches 30 days, whether it has
closed or is still open.

**A label only exists once 30 days have passed.** This is the decision the rest
of the design depends on. For an animal that arrived 12 days ago, we do not know
yet. The tempting shortcut is to keep the recent stays that have already closed
and drop the ones still open, but the ones that closed early are, by
definition, short. That would bias every recent label toward short. So maturity
is decided by intake date alone: anything within 30 days of the snapshot is
left out of training and evaluation, whether or not it has left.

**Open stays older than a year are left out** as likely missing outcomes rather
than labelled long. The feed has gaps, and labelling them would teach the model
the gaps. The count is recorded on every run.

### Features

Everything is knowable on the day the animal arrives: animal type, intake type
and condition, whether the animal is spayed or neutered, age, breed and colour
(primary component, with rare values grouped), whether the breed is mixed, where
it was found, whether it is a return stay, month and weekday of arrival, and
how many animals of the same type were already in care that morning.

That last one is built from intake and outcome events rather than a range join,
and only counts animals present before the day began. The feed cannot count
animals whose intake predates it, so the first 180 days of the feed are not
used for training.

**What is deliberately left out.** Outcome fields, obviously. Less obviously,
the animal's name. The published name can be edited after intake, and animals
that are adopted tend to get named, so it carries information from the future.
Including it would raise the score for the wrong reason.

**Sex is out; spay/neuter status is in.** The first version used both, and
sex ranked third in importance, driven almost entirely by animals recorded as
"Unknown". Only 0.4% of those became long stays, against 26 to 33% for every
other group, which is the kind of gap that can mean the field was filled in
after the fact. It was not. Half of the Unknown animals left through the
shelter-neuter-return program for community cats, and most of the rest were
transferred to partners within days, including very young litters. They are
animals staff could not examine on arrival, which is a fact available on day
one. Sex itself added nothing once spay/neuter status was in (AUC 0.726 with
it, 0.725 without), so it was dropped. Spay/neuter status was kept: removing it
cost 0.018 of AUC, and intact animals have to be altered before adoption, which
is a plausible reason on its own for a longer stay.

### Models and evaluation

A regularised logistic regression is the baseline. The main model is gradient
boosting with native handling of categories and missing values. Both are
trained on four years of history, since older years are less likely to reflect
current policies and capacity.

**Recalibration.** The long-stay rate moves. On Austin's data the 2024 to 2025
holdout ran about ten points above the long-run average, and the first version
of the model ranked animals well but predicted the old rate, three to five
points low in most deciles. So the model that is actually used holds back the
most recent six months of its training window, fits on the years before, and
then fits a slope and intercept on the log-odds against those six months
(Platt scaling). Ranking is unchanged; the probabilities move toward what the
shelter looks like now. All three versions are reported on the holdout,
including the raw model, so the trade is visible rather than asserted.

The holdout is the most recent year of mature labels, and training stops 30
days before it starts, so every training label was already known on the
holdout's first day. Without that gap the model learns from stays whose ending
had not happened yet. A random split has the same problem in a less visible
form, since stays from the same week share occupancy, season, and staffing.

Reported on the holdout: ROC AUC, average precision (more informative than AUC
when long stays are a minority), Brier score, calibration by decile, and
precision in the top 10% of scores, plus the same AUC and precision with
shelter-neuter-return transfers removed. Those cats leave within days by
design, so they are easy short stays that flatter the headline AUC. The model
cannot exclude them when scoring, since the program is decided after intake,
but the evaluation can show how much of the number they account for. Top-10%
precision is the operational number: if
staff can give extra attention to one animal in ten, how many of those would
actually have waited 30 days? Permutation importance is measured on raw inputs
so it reads in the same terms a person would use.

### Monitoring

`ml.monthly_performance` is a rolling backtest over the last twelve months.
Each month is scored by a model trained and recalibrated only on what was known
by the first of that month, which is the model staff would actually have had. Its AUC and
calibration are what they would have seen, not an in-sample figure.

A month is flagged for **review** when AUC falls below 0.65 or the predicted
long-stay rate is more than five points from the observed one. Months with
under 200 intakes are marked **insufficient_data**, because an AUC on a few
dozen animals moves more from noise than from the model.

Input drift is measured with a population stability index for each month
against **the same calendar month** in its training window. Comparing May with
a whole year would flag kitten season as drift every spring. Drift is recorded
per feature but does not flag a month on its own. A shift in who arrives only
matters if the model stops ranking them well, which the performance checks
catch, and alerting on drift alone trains people to ignore alerts. When a month
is flagged, the drift table is where to look for why.

### Checking it is not cheating

Three things guard against the model looking better than it is:

- **A leakage check on every pull request.** In the synthetic sample, length of
  stay is random with respect to every feature, so an honest model should score
  near an AUC of 0.5. `--expect-no-signal` fails the run if it scores above
  0.6. The check was verified by deliberately feeding the model the answer,
  which it caught.
- **dbt tests on the model's outputs.** The training windows are stored with
  every result, and `assert_backtest_trained_before_scored` and
  `assert_holdout_trained_before_holdout` prove the 30-day gap held rather than
  trusting the code. `assert_scores_only_for_young_open_stays` proves the list
  staff would act on contains only the animals it should.
- **A fixed row order.** The model's internal validation split is seeded, but a
  seed only makes results repeatable if the rows arrive in the same order, so
  the training query sorts them.

### What it cannot tell you

A high score says an animal resembles others that waited a long time. It does
not say why, and it does not say what would help. It is a prompt to look
sooner, not a judgement about the animal, and it should never be used to
decide which animals receive care. Breed in particular reflects how adopters
behave, including their biases, as much as anything about the animal.

The model also learns the shelter's past practices. If a policy change
shortens stays for a group the model scores as high, its scores for that group
will run high until enough new history accumulates. That is one of the things
the calibration check exists to catch.

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
| `ml.stay_scores` | One row per animal in care under 30 days | Long-stay probability and band |
| `ml.monthly_performance` | One row per backtest month | AUC, calibration, status |
| `ml.feature_drift` | Month by feature | Population stability index |
| `ml.model_evaluation` | One row per model | Holdout metrics and training windows |

## Testing

54 tests run on every build, as part of `dbt build` rather than a separate step,
so a model that breaks its own contract does not get published first and tested
afterwards. A further 28 run against the long-stay model's outputs; they sit
behind the `ml` selector so a fresh clone can build the warehouse before the
model has ever been trained.

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

`.github/workflows/pipeline.yml` runs the extract, the build, the tests, and
the model daily, publishes the dbt docs to Pages, and writes a pass/fail table
and the model's holdout metrics to the run summary.

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

**The feed is not current.** As of October 2026, the latest records the city
publishes are from 4 May 2025, and intakes and outcomes stop on the same day.
The pipeline still pulls and rebuilds daily, and every date in the model is
taken from the data rather than the clock, so "animals currently in care" in
`ml.stay_scores` means animals in care when the feed stopped. Treat the project
as a retrospective study of 2013 to 2025, not a live tool. If the feed resumes,
nothing needs to change.

Intake and outcome records published by the City of Austin under the
[Public Domain Dedication and License](https://data.austintexas.gov).
This project is not affiliated with the City of Austin or Austin Animal Center.
