with cancers as (
    select person_id,
           string_agg(distinct primary_cancer_group, ',' order by primary_cancer_group) as cancer_groups
    from {{ ref('stg_conditions') }} where primary_cancer_group is not null group by 1
),
gen as (
    select person_id, count(*) as biomarker_results,
           string_agg(biomarker_key, ',' order by biomarker_key) filter (where result = 'detected') as detected_biomarkers
    from {{ ref('stg_genomic') }} group by 1
),
meds as (select person_id, count(*) as drug_exposures from {{ source('clinical', 'drug_exposure') }} group by 1),
labs as (select person_id, count(*) as measurements, max(measured_at) as last_measurement from {{ ref('fct_measurements') }} group by 1)
select
    p.person_id, p.age_years, p.sex, p.state, p.smoking_status,
    c.cancer_groups,
    coalesce(g.biomarker_results, 0) as biomarker_results,
    g.detected_biomarkers,
    coalesce(m.drug_exposures, 0) as drug_exposures,
    coalesce(l.measurements, 0) as measurements,
    l.last_measurement
from {{ ref('stg_person') }} p
left join cancers c using (person_id)
left join gen g using (person_id)
left join meds m using (person_id)
left join labs l using (person_id)
