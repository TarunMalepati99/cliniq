-- Latest result per person x biomarker.
select distinct on (person_id, biomarker_key)
    variant_result_id, person_id, gene, biomarker_key, result, reported_date, source_record_id
from {{ source('clinical', 'genomic_variant') }}
order by person_id, biomarker_key, reported_date desc nulls last, variant_result_id
