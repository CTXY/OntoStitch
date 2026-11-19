"""Shared checks for candidate provenance and question removal; no answer selection."""
import re


def validate_initial_sql_sources(points):
    for point in points:
        options = getattr(point, "options", getattr(point, "candidates", ()))
        sql_options = []
        for index, option in enumerate(options):
            assessment = getattr(option, "evidence_assessment", None)
            types = getattr(assessment, "source_types", ())
            sources = getattr(option, "candidate_sources", getattr(option, "sources", ()))
            if "initial_sql" in types or "initial_sql" in sources or getattr(option, "initial_sql_evidence", None) or getattr(option, "sql_fragment", None):
                sql_options.append(getattr(option, "option_id", getattr(option, "candidate_id", str(index))))
        if len(sql_options) > 1:
            pid = getattr(point, "choice_point_id", getattr(point, "slot_id", "new slot"))
            raise ValueError(f"{pid}: at most one candidate may cite initial_sql; found {sql_options}. Keep that source only on the candidate describing the actual draft behavior. Alternatives require supplied graph/schema support, not hypothetical SQL fragments. Do not invent references or let code select the candidate.")


def validate_filter_basis(basis, reason, point_id, observations):
    if basis != "database_equivalence":
        return
    cited = set(re.findall(r"\bprobe:\d+\b", reason))
    by_id = {o.get("probe_id"): o for o in observations}
    if not cited:
        raise ValueError("database_equivalence requires successful database observations for this question; cite existing probe IDs in reason, or request a probe/keep the question.")
    for pid in cited:
        o = by_id.get(pid)
        result = o.get("result", {}) if o else {}
        if (not o or not o.get("success") or o.get("choice_point_id") != point_id
                or isinstance(result, dict) and (result.get("truncated") or result.get("values_truncated"))):
            raise ValueError(f"{point_id}: {pid} is absent, failed, truncated or belongs to another question. It cannot justify equivalence; request usable evidence or keep the question.")
