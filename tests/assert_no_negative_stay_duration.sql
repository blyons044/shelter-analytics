-- Durations must never be negative in the reporting layer.
-- Rows that would produce one are filtered in fct_shelter_stays and
-- quarantined in fct_data_quality_issues; this test proves that filter holds
-- rather than trusting it.

select
    stay_key,
    intake_at,
    outcome_at,
    length_of_stay_days
from {{ ref('fct_shelter_stays') }}
where length_of_stay_days < 0
