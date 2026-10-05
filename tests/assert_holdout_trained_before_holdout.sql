-- Same guarantee for the headline holdout: training ends 30 days before the
-- holdout year begins, so no training label was unknown on its first day.

select
    model,
    train_end,
    holdout_start
from {{ source('ml', 'model_evaluation') }}
where train_end > holdout_start - interval 30 day
