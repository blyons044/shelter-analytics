-- The stored live release rate must equal what its own component counts imply.
-- Guards against the macro and the counts drifting apart if either is edited.

select
    outcome_month,
    animal_type,
    live_release_rate_pct,
    outcomes_live,
    outcomes_non_live
from {{ ref('mart_monthly_outcomes') }}
where outcomes_live + outcomes_non_live > 0
  and abs(
        live_release_rate_pct
        - round(100.0 * outcomes_live / (outcomes_live + outcomes_non_live), 2)
      ) > 0.01
