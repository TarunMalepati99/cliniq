-- No landed record may remain unprocessed after a pipeline run (no silent drops).
select raw_id from {{ source('raw', 'records') }} where process_status = 'pending'
