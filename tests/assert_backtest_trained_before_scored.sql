-- Every backtest month must be scored by a model whose training data ends at
-- least 30 days before the month starts. Inside that gap, a training label
-- could depend on events after the month began, and the backtest would
-- overstate what the model can do. Stored, then proven, rather than trusted.

select
    intake_month,
    train_end
from {{ source('ml', 'monthly_performance') }}
where train_end > intake_month - interval 30 day
