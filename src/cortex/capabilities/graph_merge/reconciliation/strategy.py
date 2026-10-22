from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

import sqlglot
from sqlglot import expressions as exp

from cortex.capabilities.graph_merge.models import (
    BranchReconciliationGroup,
    EvidenceItem,
    FinalNodeGroup,
    NodeRepresentation,
    ReconcileOutcome,
    ReconciliationGroup,
)
from cortex.schema import FieldDefinition, IdentityPolicy, NodeType


class NodeReconciler(Protocol):
    def reconcile(
        self,
        group: FinalNodeGroup | BranchReconciliationGroup,
        evidence: tuple[EvidenceItem, ...],
        policy: IdentityPolicy,
    ) -> ReconcileOutcome: ...


class ReconciliationSemanticsValidator(Protocol):
    def validate(
        self,
        group: ReconciliationGroup,
        evidence: tuple[EvidenceItem, ...],
        representations: tuple[NodeRepresentation, ...],
    ) -> bool: ...


@dataclass(frozen=True)
class ReconciliationStrategy:
    reconciler: NodeReconciler | None
    validator: ReconciliationSemanticsValidator | None = None


class NodeReconcilerRegistry:
    """Resolve optional node-type reconciliation logic with a generic fallback."""

    def __init__(
        self,
        default: NodeReconciler | None,
        registrations: Mapping[
            str,
            tuple[
                frozenset[str],
                NodeReconciler,
                ReconciliationSemanticsValidator | None,
            ],
        ]
        | None = None,
    ) -> None:
        self.default = default
        self.registrations = dict(registrations or {})

    def resolve(
        self,
        node_type: NodeType,
        effective_fields: Mapping[str, FieldDefinition],
    ) -> ReconciliationStrategy:
        registration = self.registrations.get(node_type.id)
        if registration is None:
            return ReconciliationStrategy(self.default)
        required_fields, reconciler, validator = registration
        if not required_fields <= set(effective_fields):
            return ReconciliationStrategy(self.default)
        return ReconciliationStrategy(reconciler, validator)


class MetricReconciliationSemanticsValidator:
    """Reject implementation introspection masquerading as a Metric applicability boundary."""

    _IMPLEMENTATION_SELECTOR_TOKENS = (
        "expression",
        "expr",
        "formula",
        "sql",
        "generation_plan",
        "implementation",
        "公式",
        "表达式",
        "实现",
    )
    _FORMULA_SEMANTIC_TOKENS = (
        "formula",
        "expression",
        "calculation",
        "aggregation",
        "公式",
        "表达式",
        "计算",
        "聚合",
    )

    def validate(
        self,
        group: ReconciliationGroup,
        evidence: tuple[EvidenceItem, ...],
        representations: tuple[NodeRepresentation, ...],
    ) -> bool:
        if group.relation != "compatible":
            return True
        evidence_by_source = {item.source: item for item in evidence}
        representation_by_ref = {item.ref: item for item in representations}
        for residual in group.residuals:
            semantic_point = residual.semantic_point.casefold()
            if not any(token in semantic_point for token in self._FORMULA_SEMANTIC_TOKENS):
                continue
            if not residual.applies_when:
                return False
            selector = residual.applies_when.casefold()
            if any(token in selector for token in self._IMPLEMENTATION_SELECTOR_TOKENS):
                return False
            supported = tuple(
                evidence_by_source[source]
                for ref in residual.supporting_refs
                if ref in representation_by_ref
                for source in representation_by_ref[ref].sources
                if source in evidence_by_source
            )
            other = tuple(
                evidence_by_source[source]
                for ref in group.supporting_refs
                if ref not in residual.supporting_refs and ref in representation_by_ref
                for source in representation_by_ref[ref].sources
                if source in evidence_by_source
            )
            supported_predicates = request_scope_predicates(supported)
            distinguishing_predicates = supported_predicates - request_scope_predicates(other)
            distinguishing_groups = request_scope_grouping_fields(
                supported
            ) - request_scope_grouping_fields(other)
            predicate_match = any(
                column.casefold() in selector and value.casefold() in selector
                for column, value in distinguishing_predicates
            )
            grouping_match = any(
                field.casefold() in selector for field in distinguishing_groups
            )
            if (distinguishing_predicates or distinguishing_groups) and not (
                predicate_match or grouping_match
            ):
                return False
        return True


class GenerationRuleReconciliationSemanticsValidator:
    """Reject unselected compression of result-changing count operands."""

    _COUNT_OPERAND_POINTS = ("reference", "operand", "count", "identifier")

    def validate(
        self,
        group: ReconciliationGroup,
        evidence: tuple[EvidenceItem, ...],
        representations: tuple[NodeRepresentation, ...],
    ) -> bool:
        del evidence
        if group.relation != "compatible":
            return True
        members = {
            item.ref: item
            for item in representations
            if item.ref in set(group.supporting_refs)
        }
        if not members or any(
            item.content.properties.get("operation_intent") != "count_aggregation"
            for item in members.values()
        ):
            return True
        refs_by_signature: dict[tuple[str, ...], set] = {}
        for ref, item in members.items():
            signature = tuple(
                sorted(
                    value
                    for value in item.content.properties.get("references", ())
                    if isinstance(value, str) and value.startswith("column.")
                )
            )
            refs_by_signature.setdefault(signature, set()).add(ref)
        if len(refs_by_signature) <= 1:
            return True

        residual_positions = {
            frozenset(residual.supporting_refs)
            for residual in group.residuals
            if residual.applies_when
            and any(
                token in residual.semantic_point.casefold()
                for token in self._COUNT_OPERAND_POINTS
            )
        }
        expected_positions = {
            frozenset(refs) for refs in refs_by_signature.values()
        }
        has_conditional_relation = any(
            relation.relation == "conditional" for relation in group.residual_relations
        )
        return expected_positions <= residual_positions and has_conditional_relation


def request_scope_predicates(
    evidence: tuple[EvidenceItem, ...],
) -> set[tuple[str, str]]:
    predicates: set[tuple[str, str]] = set()
    for item in evidence:
        sql = item.content.get("sql")
        if not isinstance(sql, str):
            continue
        try:
            tree = sqlglot.parse_one(sql)
        except sqlglot.errors.ParseError:
            continue
        for where in tree.find_all(exp.Where):
            for equality in where.this.find_all(exp.EQ):
                column = equality.left
                value = equality.right
                if isinstance(column, exp.Column) and isinstance(value, exp.Literal):
                    predicates.add((column.name, str(value.this)))
    return predicates


def request_scope_candidates(
    group: FinalNodeGroup | BranchReconciliationGroup,
    evidence: tuple[EvidenceItem, ...],
) -> tuple[dict[str, object], ...]:
    representations = (
        group.members
        if isinstance(group, FinalNodeGroup)
        else (group.root, *group.incoming_members)
    )
    return representation_scope_candidates(representations, evidence)


def representation_scope_candidates(
    representations: tuple[NodeRepresentation, ...],
    evidence: tuple[EvidenceItem, ...],
) -> tuple[dict[str, object], ...]:
    evidence_by_source = {item.source: item for item in evidence}
    rows = []
    for representation in representations:
        representation_evidence = tuple(
            evidence_by_source[source]
            for source in representation.sources
            if source in evidence_by_source
        )
        predicates = sorted(request_scope_predicates(representation_evidence))
        grouping_fields = sorted(request_scope_grouping_fields(representation_evidence))
        rows.append(
            {
                "representation_ref": representation.ref.model_dump(mode="json"),
                "questions": [
                    str(item.content["question"])
                    for item in representation_evidence
                    if item.content.get("question")
                ],
                "predicates": [
                    {"field": field, "operator": "=", "value": value}
                    for field, value in predicates
                ],
                "grouping_fields": grouping_fields,
            }
        )
    return tuple(rows)


def request_scope_grouping_fields(
    evidence: tuple[EvidenceItem, ...],
) -> set[str]:
    """Extract observed SQL grouping fields without deciding their ontology ownership."""

    fields = set()
    for item in evidence:
        sql = item.content.get("sql")
        if not isinstance(sql, str):
            continue
        try:
            tree = sqlglot.parse_one(sql)
        except sqlglot.errors.ParseError:
            continue
        for group in tree.find_all(exp.Group):
            fields.update(expression.sql() for expression in group.expressions)
    return fields


__all__ = [
    "GenerationRuleReconciliationSemanticsValidator",
    "MetricReconciliationSemanticsValidator",
    "NodeReconciler",
    "NodeReconcilerRegistry",
    "ReconciliationSemanticsValidator",
    "ReconciliationStrategy",
    "representation_scope_candidates",
    "request_scope_candidates",
    "request_scope_grouping_fields",
    "request_scope_predicates",
]
