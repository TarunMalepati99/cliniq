# ADR 0002 — Four-valued evidence instead of boolean matching

**Status:** accepted

## Context
Real-world data is incomplete. A boolean `WHERE` clause silently treats "never tested for EGFR" the same
as "EGFR negative", which biases feasibility estimates downward and hides recoverable patients.

## Decision
Each criterion evaluates to one of four evidence types:
- **positive**: a matching record exists.
- **negative**: there is explicit contradicting evidence.
- **absent**: the patient has records in the domain, but none match.
- **unknown**: there is no data in the domain.

The status mapping is positive → met; negative or absent → not met; unknown → unknown. Patients with
unknowns and no disqualifier are classified *potentially eligible*, and the system reports which
criterion blocks them.

## Consequences
- ➕ Feasibility reports separate "not eligible" from "not knowable yet", which is what chart-review planning needs.
- ➕ Histology-undocumented lung cancer, indeterminate tests and missing labs are handled explicitly.
- ➖ *Absent* is a judgment call (it counts as not met). We count ineligibility that rests only on absent evidence and show it in the UI.
- ➖ The SQL is more complex than a filter. Per-person lateral subqueries needed indexes (migration 004) to reach about 100 ms on 1.2k patients; very large populations would need set-based rewrites or materialized evidence tables.
