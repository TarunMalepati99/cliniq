"""Static + dynamic validation of compiled SQL before it can be approved or executed.

Defense in depth — the compiler already only emits allowlisted SQL, but we verify the
artifact itself (an LLM never writes SQL here, yet a compiler bug must not become a hole):
  1. exactly one statement, and it is a SELECT
  2. only allowlisted schema.table references (CTE names excepted)
  3. no non-allowlisted functions (pg_*, dblink, set_config, ...)
  4. no literal string values outside a fixed set (values must be bind parameters)
  5. EXPLAIN succeeds as the read-only role (catches type errors without executing)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import sqlglot
from sqlglot import exp

ALLOWED_TABLES = {("clinical", t) for t in ("person", "condition_occurrence", "measurement", "drug_exposure",
                                            "procedure_occurrence", "genomic_variant", "note", "note_fact")}
ALLOWED_ANON_FUNCS = {"age", "date_part", "jsonb_build_object", "jsonb_agg"}
FORBIDDEN_NODES = (exp.Insert, exp.Update, exp.Delete, exp.Drop, exp.Create, exp.Alter, exp.Command, exp.Merge,
                   exp.Grant, exp.TruncateTable, exp.Copy, exp.Into)
ALLOWED_LITERALS = {"unknown", "positive", "negative", "absent", "detected", "not_detected", "indeterminate", "year",
                    "ecog", "measurement", "note_text", "", "age_years", "sex", "state", "value", "observed", "ref",
                    "provenance", "source_record_id"}


@dataclass
class ValidationReport:
    ok: bool
    checks: list[dict[str, Any]] = field(default_factory=list)

    def add(self, name: str, passed: bool, detail: str = "") -> None:
        self.checks.append({"check": name, "passed": passed, "detail": detail})
        self.ok = self.ok and passed


def validate_sql(sql: str) -> ValidationReport:
    rep = ValidationReport(ok=True)
    try:
        stmts = [s for s in sqlglot.parse(sql, read="postgres") if s is not None]
    except sqlglot.errors.ParseError as e:
        rep.add("parse", False, str(e)[:300])
        return rep
    rep.add("parse", True)
    rep.add("single_statement", len(stmts) == 1, f"{len(stmts)} statements")
    if not stmts:
        return rep
    tree = stmts[0]
    rep.add("select_only", isinstance(tree, exp.Select) and not any(tree.find_all(*FORBIDDEN_NODES)), type(tree).__name__)
    cte_names = {c.alias_or_name for c in tree.find_all(exp.CTE)}
    tables = {(t.db, t.name) for t in tree.find_all(exp.Table) if not (t.db == "" and t.name in cte_names)}
    bad_tables = sorted(f"{d}.{n}" for d, n in tables - ALLOWED_TABLES)
    rep.add("allowlisted_tables", not bad_tables, ", ".join(bad_tables) or f"{len(tables)} tables")
    anon = {f.name.lower() for f in tree.find_all(exp.Anonymous)}
    bad_funcs = sorted(anon - ALLOWED_ANON_FUNCS)
    rep.add("allowlisted_functions", not bad_funcs, ", ".join(bad_funcs))
    keys_ok = {"matching_conditions", "conditions_on_record", "site_match_histology_undocumented", "site_match_other_histology",
               "condition_id", "code", "histology", "results", "markers_tested", "variant_result_id", "biomarker", "result",
               "reported_date", "matching_exposures", "medication_records", "drug_exposure_id", "drug", "class", "start_date",
               "matching_procedures", "procedure_records", "procedure_id", "date"}
    literals = {lit.this for lit in tree.find_all(exp.Literal) if lit.is_string}
    bad_lits = sorted(literals - ALLOWED_LITERALS - keys_ok)
    rep.add("no_inline_values", not bad_lits, ", ".join(bad_lits[:5]))
    return rep


def explain(sql: str, params: dict[str, Any], conn: Any) -> tuple[bool, str]:
    try:
        plan = conn.execute("EXPLAIN " + sql, params).fetchall()
        return True, f"plan ok ({len(plan)} lines)"
    except Exception as exc:  # noqa: BLE001 - surfaced to the user verbatim
        return False, f"{type(exc).__name__}: {str(exc)[:300]}"
