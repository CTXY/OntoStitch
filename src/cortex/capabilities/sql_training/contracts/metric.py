from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass

from cortex.capabilities.sql_training.core.enums import NodeType
from cortex.capabilities.sql_training.models import NodeAsset

# Python's ``\w`` is Unicode-aware. Keep placeholder names identifier-like while
# allowing business-language names such as ``${零售额}``.
_PLACEHOLDER_RE = re.compile(r"\$\{([^\W\d]\w*)\}")
_TABLE_TOKEN_RE = re.compile(r"<([^<>\s.]+)>")
# SQL Column identifiers may legitimately contain spaces, parentheses, and dashes.
# The table part remains unambiguous; split the physical token at its first dot.
_COLUMN_TOKEN_RE = re.compile(r"<([^<>\s.]+)\.([^<>.]+)>")
_ANGLE_TOKEN_RE = re.compile(r"<([^<>]+)>")
_SQL_SELECT_FROM_RE = re.compile(
    r"\bselect\b(?P<projection>[\s\S]*?)\bfrom\b",
    re.IGNORECASE,
)
_QUERY_OPERATION_RE = re.compile(r"\border\s+by\b|\blimit\b", re.IGNORECASE)
_ONTOLOGY_REF_RE = re.compile(
    r"\b(?:column|dimension_value|example|generation_rule|join_path|metric|"
    r"concept|table)\.[^\s<>,;()]+",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class MetricDependencyDiagnostic:
    code: str
    source_metric_id: str
    target_metric_ids: tuple[str, ...] = ()
    cycle_path: tuple[str, ...] = ()

    def as_feedback(self) -> dict[str, object]:
        return {
            "code": self.code,
            "source_metric_id": self.source_metric_id,
            "target_metric_ids": list(self.target_metric_ids),
            "cycle_path": list(self.cycle_path),
        }


def metric_dependency_refs(asset: NodeAsset) -> set[str]:
    if asset.node_type != NodeType.METRIC:
        return set()
    return {
        str(reference).strip()
        for reference in [
            *asset.properties.references,
            *asset.properties.expression.refs.values(),
        ]
        if str(reference).strip().startswith("metric.")
    }


def metric_dependency_diagnostics(
    current_metrics: list[NodeAsset] | tuple[NodeAsset, ...],
    candidate_metrics: list[NodeAsset] | tuple[NodeAsset, ...],
) -> list[MetricDependencyDiagnostic]:
    """Validate the candidate-over-current Metric overlay without mutating either input."""

    current = {asset.id: asset for asset in current_metrics if asset.node_type == NodeType.METRIC}
    candidates = {
        asset.id: asset for asset in candidate_metrics if asset.node_type == NodeType.METRIC
    }
    metrics = {**current, **candidates}
    diagnostics: list[MetricDependencyDiagnostic] = []
    dependencies: dict[str, set[str]] = {}
    for metric_id, asset in metrics.items():
        refs = metric_dependency_refs(asset)
        unknown = tuple(sorted(refs - set(metrics)))
        if unknown:
            diagnostics.append(
                MetricDependencyDiagnostic("MISSING_METRIC_DEPENDENCY", metric_id, unknown)
            )
        if metric_id in refs:
            diagnostics.append(
                MetricDependencyDiagnostic(
                    "METRIC_SELF_REFERENCE", metric_id, (metric_id,), (metric_id, metric_id)
                )
            )
        dependencies[metric_id] = refs & set(metrics) - {metric_id}

    visiting: list[str] = []
    visited: set[str] = set()
    seen_cycles: set[tuple[str, ...]] = set()

    def visit(metric_id: str) -> None:
        if metric_id in visiting:
            start = visiting.index(metric_id)
            cycle = tuple([*visiting[start:], metric_id])
            if cycle not in seen_cycles:
                seen_cycles.add(cycle)
                diagnostics.append(
                    MetricDependencyDiagnostic(
                        "METRIC_DEPENDENCY_CYCLE", metric_id, cycle[:-1], cycle
                    )
                )
            return
        if metric_id in visited:
            return
        visiting.append(metric_id)
        for dependency in sorted(dependencies.get(metric_id, set())):
            visit(dependency)
        visiting.pop()
        visited.add(metric_id)

    for metric_id in sorted(metrics):
        visit(metric_id)
    return diagnostics


def invalid_candidate_metric_ids(
    diagnostics: list[MetricDependencyDiagnostic],
    candidate_metrics: list[NodeAsset] | tuple[NodeAsset, ...],
) -> set[str]:
    """Return invalid candidate roots and candidate dependents; never remove current Metrics."""

    candidates = {asset.id: asset for asset in candidate_metrics}
    invalid = {
        metric_id
        for diagnostic in diagnostics
        for metric_id in (diagnostic.source_metric_id, *diagnostic.target_metric_ids)
        if metric_id in candidates
    }
    changed = True
    while changed:
        changed = False
        for metric_id, asset in candidates.items():
            if metric_id not in invalid and metric_dependency_refs(asset) & invalid:
                invalid.add(metric_id)
                changed = True
    return invalid


def validate_metric_contract(
    asset: NodeAsset,
    *,
    require_generation_plan: bool,
    require_pattern: bool = False,
) -> None:
    """Validate reusable Metric expression and SQL-generation guidance contracts."""

    if asset.node_type != NodeType.METRIC:
        raise ValueError("Metric contract validation requires a Metric asset")
    expression = asset.properties.expression
    placeholders = set(_PLACEHOLDER_RE.findall(expression.expr))
    ref_names = set(expression.refs)
    if placeholders != ref_names:
        raise ValueError(
            "Metric expression placeholders must exactly match expression.refs keys; "
            "use ${key} in expression.expr and bare key in expression.refs (not {key}, <key>, or bare SQL names); "
            f"missing_refs={sorted(placeholders-ref_names)}, "
            f"unused_refs={sorted(ref_names-placeholders)}"
        )
    if any("${" in name or "}" in name for name in expression.refs):
        raise ValueError("Metric expression.refs keys must be bare variable names")
    pattern = asset.properties.pattern
    if require_pattern and not pattern:
        raise ValueError("Metric pattern must be non-empty")
    pattern_placeholders = set(_PLACEHOLDER_RE.findall(pattern))
    unknown_pattern_placeholders = sorted(pattern_placeholders - ref_names)
    if unknown_pattern_placeholders:
        raise ValueError(
            "Metric pattern placeholders must resolve through expression.refs; "
            f"unknown={unknown_pattern_placeholders}"
        )
    declared_refs = set(asset.properties.references)
    missing_declared_refs = sorted(set(expression.refs.values()) - declared_refs)
    if missing_declared_refs:
        raise ValueError(
            f"Metric references omit expression dependencies: {missing_declared_refs}"
        )
    extra_declared_refs = sorted(declared_refs - set(expression.refs.values()))
    if extra_declared_refs:
        raise ValueError(
            "Metric references contain dependencies not used by expression: "
            f"{extra_declared_refs}"
        )
    metric_texts = [pattern] if pattern else []
    _validate_schema_tokens(metric_texts, field_name="Metric pattern")
    _validate_reusable_guidance(metric_texts, field_name="Metric pattern")

    plans = asset.properties.sql_generation_plans
    if not plans:
        if require_generation_plan:
            raise ValueError("Metric sql_generation_plans requires at least one plan")
        return

    duplicate_ids = sorted(
        plan_id
        for plan_id, count in Counter(plan.id for plan in plans).items()
        if count > 1
    )
    if duplicate_ids:
        raise ValueError(
            "Metric sql_generation_plans contain duplicate ids: "
            f"{duplicate_ids}"
        )
    normalized_applications = [_normalize_application(plan.application) for plan in plans]
    duplicate_applications = sorted(
        application
        for application, count in Counter(normalized_applications).items()
        if count > 1
    )
    if duplicate_applications:
        raise ValueError(
            "Metric sql_generation_plans contain duplicate applications: "
            f"{duplicate_applications}"
        )
    for plan in plans:
        plan_texts = [plan.application, *plan.steps]
        _validate_schema_tokens(plan_texts, field_name="Metric sql_generation_plans")
        _validate_reusable_guidance(
            plan_texts,
            field_name="Metric sql_generation_plans",
        )


def validate_metric_dependency_graph(assets: list[NodeAsset]) -> None:
    """Validate exact Metric dependencies and reject dependency cycles."""

    diagnostics = metric_dependency_diagnostics([], assets)
    if not diagnostics:
        return
    diagnostic = diagnostics[0]
    if diagnostic.code == "MISSING_METRIC_DEPENDENCY":
        raise ValueError(
            f"Metric dependencies not found for {diagnostic.source_metric_id}: "
            f"{list(diagnostic.target_metric_ids)}"
        )
    if diagnostic.code == "METRIC_SELF_REFERENCE":
        raise ValueError(f"Metric cannot reference itself: {diagnostic.source_metric_id}")
    raise ValueError(f"Metric dependency cycle: {' -> '.join(diagnostic.cycle_path)}")


def _normalize_application(value: str) -> str:
    return re.sub(r"[\W_]+", "", value, flags=re.UNICODE).casefold()


def _validate_schema_tokens(texts: list[str], *, field_name: str) -> None:
    for text in texts:
        for token in _ANGLE_TOKEN_RE.findall(text):
            wrapped = f"<{token}>"
            if (
                _TABLE_TOKEN_RE.fullmatch(wrapped) is None
                and _COLUMN_TOKEN_RE.fullmatch(wrapped) is None
            ):
                raise ValueError(
                    f"{field_name} schema references must use <table> or "
                    f"<table.column>: <{token}>"
                )


def _validate_reusable_guidance(texts: list[str], *, field_name: str) -> None:
    for text in texts:
        if _ONTOLOGY_REF_RE.search(text):
            raise ValueError(
                f"{field_name} must use physical <table> or <table.column> "
                "tokens instead of ontology references"
            )
        if _contains_complete_sql(text):
            raise ValueError(f"{field_name} must not contain complete SQL")
        if _QUERY_OPERATION_RE.search(text):
            raise ValueError(f"{field_name} must not contain ordering or limits")


def _contains_complete_sql(text: str) -> bool:
    """Reject SQL statements without mistaking ordinary 'select ... from ...' prose for SQL."""

    match = _SQL_SELECT_FROM_RE.search(text)
    if match is None:
        return False
    projection = match.group("projection").strip()
    if re.search(r"\b(?:a|an|the)\b", projection, flags=re.IGNORECASE):
        return False
    return bool(
        re.search(
            r"(?:\b(?:avg|count|max|min|sum)\s*\(|\b(?:case|distinct)\b|"
            r"<[^<>]+>|\$\{[^{}]+\}|\*)",
            projection,
            flags=re.IGNORECASE,
        )
    )
