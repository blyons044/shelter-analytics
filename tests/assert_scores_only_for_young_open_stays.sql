-- The model scores animals still in care for under 30 days. A score on a
-- closed stay, or on one already past 30 days, means the scoring population
-- was built wrong and the list staff would act on is not the list intended.

select
    s.stay_key,
    s.days_in_care,
    f.is_open_stay
from {{ source('ml', 'stay_scores') }} s
left join {{ ref('fct_shelter_stays') }} f
    on f.stay_key = s.stay_key
where f.stay_key is null
   or not f.is_open_stay
   or s.days_in_care >= 30
