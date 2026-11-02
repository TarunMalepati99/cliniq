-- Fails if any stored lab value lies outside the plausibility window enforced at ingestion.
select * from {{ ref('fct_measurements') }}
where (lab_key = 'creatinine' and value_numeric not between 0.1 and 20)
   or (lab_key = 'hemoglobin' and value_numeric not between 2 and 25)
   or (lab_key = 'pdl1_tps' and value_numeric not between 0 and 100)
   or (lab_key = 'ecog' and value_numeric not between 0 and 5)
