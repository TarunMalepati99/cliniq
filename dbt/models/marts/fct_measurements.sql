{{ config(materialized='incremental', unique_key='measurement_id') }}
-- Incremental: raw record ids are monotonically increasing, so new loads have higher ids.
select measurement_id, person_id, lab_key, value_numeric, unit, measured_at, source_record_id
from {{ source('clinical', 'measurement') }}
{% if is_incremental() %}
where source_record_id > (select coalesce(max(source_record_id), 0) from {{ this }})
{% endif %}
