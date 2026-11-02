-- Share of persons with at least one record per data domain (field-level completeness for person).
with n as (select count(*)::numeric as total from {{ source('clinical', 'person') }})
select 'person.birth_date' as domain, count(*) filter (where birth_date is not null) as persons_with_data, (select total from n) as total_persons from {{ source('clinical', 'person') }}
union all select 'person.state', count(*) filter (where state is not null), (select total from n) from {{ source('clinical', 'person') }}
union all select 'conditions', count(distinct person_id), (select total from n) from {{ source('clinical', 'condition_occurrence') }}
union all select 'measurements', count(distinct person_id), (select total from n) from {{ source('clinical', 'measurement') }}
union all select 'drug_exposures', count(distinct person_id), (select total from n) from {{ source('clinical', 'drug_exposure') }}
union all select 'genomic_results', count(distinct person_id), (select total from n) from {{ source('clinical', 'genomic_variant') }}
union all select 'notes', count(distinct person_id), (select total from n) from {{ source('clinical', 'note') }}
union all select 'ecog (structured or note)', count(distinct person_id), (select total from n) from (
    select person_id from {{ source('clinical', 'measurement') }} where lab_key = 'ecog'
    union select person_id from {{ source('clinical', 'note_fact') }} where fact_key = 'ecog') e
