select condition_id from {{ ref('stg_conditions') }} where onset_date > current_date
