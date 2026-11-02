select biomarker_key, gene,
       count(*) as persons_tested,
       count(*) filter (where result = 'detected') as detected,
       count(*) filter (where result = 'not_detected') as not_detected,
       count(*) filter (where result = 'indeterminate') as indeterminate
from {{ ref('stg_genomic') }}
group by 1, 2
