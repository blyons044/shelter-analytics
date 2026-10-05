-- An open stay with an outcome attached means the flag and the data disagree,
-- which would quietly distort both the live release rate and average duration.

select
    stay_key,
    is_open_stay,
    outcome_at,
    outcome_type
from {{ ref('fct_shelter_stays') }}
where (is_open_stay and outcome_at is not null)
   or (not is_open_stay and outcome_at is null)
