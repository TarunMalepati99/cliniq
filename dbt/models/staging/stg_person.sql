select
    person_id,
    birth_date,
    case when birth_date is null then null
         else date_part('year', age(date '{{ var("as_of_date") }}', birth_date))::int end as age_years,
    sex,
    state,
    smoking_status,
    source_record_id
from {{ source('clinical', 'person') }}
