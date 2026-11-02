select
    c.condition_id,
    c.person_id,
    c.code,
    coalesce(k.name, 'UNMAPPED') as condition_name,
    c.histology_code,
    case
        when c.code like 'C34%' and c.histology_code in ('8140/3','8070/3','8012/3','8046/3') then 'nsclc'
        when c.code like 'C34%' and c.histology_code = '8041/3' then 'sclc'
        when c.code like 'C34%' then 'lung_unspecified_histology'
        when c.code like 'C50%' then 'breast_cancer'
        when c.code like 'C18%' or c.code like 'C19%' or c.code like 'C20%' then 'colorectal_cancer'
        when c.code like 'C25%' then 'pancreatic_cancer'
        when c.code like 'C43%' then 'melanoma'
        when c.code like 'C61%' then 'prostate_cancer'
    end as primary_cancer_group,
    c.onset_date,
    c.source_record_id
from {{ source('clinical', 'condition_occurrence') }} c
left join {{ source('clinical', 'concept') }} k on k.vocabulary = c.vocabulary and k.code = c.code
