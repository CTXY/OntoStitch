from __future__ import annotations

import re
from collections.abc import Mapping
from enum import Enum
from typing import Literal, Self

import sqlglot
from langchain_core.language_models.chat_models import BaseChatModel
from pydantic import BaseModel, ConfigDict, Field, create_model, model_validator
from sqlglot import expressions

from cortex.application.retrieve_db import NodeCandidate, NodeSelection
from cortex.capabilities.retrieve_db.normalization import normalize_text
from cortex.capabilities.retrieve_db.reference_conflicts import reference_contract
from cortex.infrastructure.llm import run_llm_extraction
from cortex.schema import GraphNode, NodeMergeConflict


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CandidateDecision(_Model):
    candidate_id: str
    status: Literal["selected", "rejected", "ambiguous"]
    reason: str = Field(description="Short, candidate-specific reason for this decision.")


class SelectionOutput(_Model):
    decisions: tuple[CandidateDecision, ...]

    @model_validator(mode="after")
    def validate_partition(self) -> Self:
        supplied = tuple(item.candidate_id for item in self.decisions)
        if len(set(supplied)) != len(supplied):
            raise ValueError("selector candidate IDs must be unique")
        if any(not item.reason.strip() for item in self.decisions):
            raise ValueError("every selector decision requires a reason")
        return self


class CandidateSelector:
    def __init__(self, llm: BaseChatModel, *, policy: str = "training_recall") -> None:
        if policy not in {"training_recall", "unattended"}:
            raise ValueError(f"unknown retrieve-db selection policy: {policy}")
        self._llm = llm
        self._policy = policy

    async def select(
        self,
        *,
        question: str,
        initial_sql: str | None,
        candidates: tuple[NodeCandidate, ...],
        concept_value_ids: Mapping[str, frozenset[str]] | None = None,
        candidate_nodes: Mapping[str, GraphNode] | None = None,
        node_conflicts: Mapping[str, NodeMergeConflict] | None = None,
    ) -> tuple[NodeSelection, ...]:
        if not candidates:
            return ()
        output = await run_llm_extraction(
            llm=self._llm,
            prompt=(
                _unattended_prompt()
                + (
                    "\nUnresolved reference alternatives are bundles of historical knowledge, "
                    "not confirmed implementations. For such a bundle, selected means relevant "
                    "reference information: select it when at least one complete alternative "
                    "applies to the question. Do not resolve, combine or rewrite alternatives; "
                    "the downstream reasoning agent assesses their use. Apply the preceding "
                    "safe-use rules to ordinary assets."
                    if node_conflicts
                    else ""
                )
                if self._policy == "unattended"
                else "Decide which recalled ontology candidates are needed to understand and "
                "answer "
                "the Question. Assess every candidate and prefer complete recall over premature "
                "consolidation.\n\n"
                "Evidence:\n"
                "- The Question is the primary evidence for requested meaning and behavior.\n"
                "- A candidate's selection_contract states what that asset owns.\n"
                "- The initial SQL is an untrusted record of attempted behavior. Use it only to "
                "identify that behavior or match a declared SQL Column/operation trigger. It "
                "cannot define a "
                "Question term, add a qualifier absent from the Question, or negate a clear "
                "ontology contract.\n\n"
                "Selection rules:\n"
                "1. Keep every candidate with reasonable Question support whose contract applies.\n"
                "2. Select all compatible candidates, even when they overlap, share a physical "
                "target, or describe related parts of the same phrase.\n"
                "3. Concept, Metric, and Generation Rule have different ownership and do not "
                "replace one another. Keep a named Concept or Metric together with every "
                "applicable Rule that acts on it.\n"
                "For a Generation Rule, treat Question phrases, SQL Columns, and SQL operations "
                "only as recall signals, then decide exact applicability from its condition. "
                "When the condition holds, select the Rule and treat its action as the governed "
                "behavior. Do not reject it because the action differs from an unaided reading of "
                "the Question; asset-quality review owns that judgment.\n"
                "4. Do not reject a candidate only because another candidate uses similar words, "
                "references the same node, or covers another part of the phrase.\n"
                "5. If several interpretations can all be true, select all of them. If they "
                "conflict and the evidence cannot decide, mark each conflicting candidate "
                "ambiguous instead of guessing.\n"
                "6. Reject only with a specific reason: the Question does not express the meaning, "
                "a Rule condition does not hold, the contract contradicts an explicit requirement "
                "outside a matching Rule's governed behavior, "
                "the candidate contract is incomplete, a competing contract creates a real "
                "conflict, or recall came only from a weak substring, graph edge, or initial-SQL "
                "match. A different table, Column, join, or calculation in the initial SQL is not "
                "rejection evidence by itself.\n"
                "7. Prefer a matching semantic asset over raw Columns or Dimension Values that "
                "only implement it. Dependency closure supplies those physical nodes. Select a raw "
                "Column separately only when the Question names that field or another applicable "
                "contract requires it.\n"
                "8. A nested phrase is independent only when the Question states that meaning. An "
                "outer filter, comparison, minimum, maximum, ranking, or grouping operation does "
                "not suppress its named Concept or Metric.\n"
                "9. For generic time scope without a named date meaning, prefer the configured "
                "default-time Column. Select another time Column only when the Question names "
                "it.\n\n"
                "Return one decision for every supplied candidate. Copy each candidate ID exactly. "
                "Use selected for an applicable candidate, rejected only with concrete evidence, "
                "and ambiguous only for a real unresolved conflict. Keep each reason short."
            ),
            output_model=_selection_output_model(candidates),
            human_payload={
                "question": question,
                "initial_sql": initial_sql or "",
                "candidates": [
                    {
                        **item.model_dump(mode="json"),
                        "selection_contract": _selection_contract(
                            (candidate_nodes or {}).get(item.node_id)
                        )
                        if item.node_id not in (node_conflicts or {})
                        else reference_contract(
                            (candidate_nodes or {})[item.node_id],
                            node_conflicts[item.node_id],
                            _selection_contract,
                        ),
                    }
                    for item in candidates
                ],
            },
        )
        decisions = {_decision_candidate_id(item): item for item in output.decisions}
        selections = tuple(
            NodeSelection(
                candidate_id=item.candidate_id,
                status=decisions[item.candidate_id].status,
                role=_candidate_role(item),
                reason=decisions[item.candidate_id].reason,
            )
            for item in candidates
        )
        if self._policy == "unattended":
            return selections
        intent_seeded = _seed_canonical_intent_rules(candidates, selections)
        seeded = _seed_unique_exact_semantic_candidates(
            candidates,
            intent_seeded,
            candidate_nodes=candidate_nodes or {},
        )
        preferred = _prefer_matching_concepts(
            candidates,
            seeded,
            concept_value_ids=concept_value_ids or {},
        )
        concept_filtered = _enforce_concept_owned_mentions(
            candidates,
            preferred,
            candidate_nodes=candidate_nodes or {},
        )
        literal_filtered = _enforce_complete_literal_binding(
            question,
            initial_sql,
            candidates,
            concept_filtered,
            candidate_nodes=candidate_nodes or {},
        )
        grain_filtered = _enforce_explicit_month_grain(
            question,
            candidates,
            literal_filtered,
            candidate_nodes=candidate_nodes or {},
        )
        explicit_filtered = _enforce_explicit_column_labels(
            question,
            candidates,
            grain_filtered,
            candidate_nodes=candidate_nodes or {},
        )
        return _enforce_concept_literal_column_binding(
            initial_sql,
            candidates,
            explicit_filtered,
            candidate_nodes=candidate_nodes or {},
        )


def _unattended_prompt() -> str:
    return (
        "Select ontology candidates that are safe to use without a user or agent resolving "
        "ambiguity. Assess every candidate independently, but consider competing candidates "
        "together. Precision is more important than returning an asset.\n\n"
        "Evidence authority:\n"
        "- The Question and its supplied task evidence define requested meaning and behavior.\n"
        "- A candidate's selection_contract defines the complete behavior owned by that asset.\n"
        "- Initial SQL is an untrusted attempt. It may reveal a SQL role or physical reference, "
        "but cannot prove business meaning, settle a conflict, or make an asset applicable.\n\n"
        "Decision rules:\n"
        "1. Use selected only when the complete asset contract is directly applicable and no "
        "unresolved user choice is needed. Partial topical relevance is not enough.\n"
        "First identify the exact requested outputs, filters, calculations, grouping, ranking, "
        "and row selection from the Question and its Evidence. An asset must own one of those "
        "requested roles; being useful only as a join key or implementation detail is not enough.\n"
        "2. For a Concept, require the requested meaning and physical binding to match. If "
        "several targets or near-synonym fields remain possible, mark the competing candidates "
        "ambiguous rather than choosing one. Do not infer that offered means served, street means "
        "mailing street, city means county, or another close label is equivalent unless supplied "
        "evidence or the asset explicitly establishes that equivalence.\n"
        "3. For a Metric, verify its complete numerator, denominator, population, grain, "
        "aggregation, unit or scale, and required guards. A metric that matches only some "
        "operands or the general topic is rejected unless it forms a real unresolved conflict, "
        "in which case mark it ambiguous. Compare thresholds in the Question with the metric's "
        "scale: a fraction and a percentage-points expression are not interchangeable.\n"
        "4. For a Generation Rule, select it only when its complete condition and prerequisites "
        "hold. Similar operations or trigger columns are recall evidence only. Distinguish "
        "global from group-wise ranking, ordinal from top-N selection, and one-row from all-ties "
        "behavior.\n"
        "5. Raw Columns and Dimension Values are selected only when their exact physical meaning "
        "or encoded value is established for a requested local role. Supporting schema nodes are "
        "not substitutes for a semantic asset.\n"
        "A projection Concept or projection Rule applies only when the Question requests that "
        "field in the result; filtering, joining, or merely mentioning an entity does not request "
        "its name or related display fields.\n"
        "6. If two complete implementations conflict and the Question evidence cannot decide, "
        "mark every competing candidate ambiguous. Never use Initial SQL as the tie breaker.\n"
        "If explicit Question Evidence supplies a formula, field equivalence, encoded value, or "
        "entity definition, it overrides a conflicting candidate for this retrieval request: "
        "reject the conflicting candidate rather than calling it applicable.\n"
        "7. Reject candidates that are merely partial references, supporting context, weak "
        "substring or embedding matches, or close-but-different meanings. State the missing or "
        "mismatched applicability dimension in the reason.\n"
        "8. Select compatible assets together only when each asset independently passes these "
        "rules. Do not select an asset merely because another selected asset references it; "
        "dependency closure is handled after selection.\n\n"
        "Return one decision for every supplied candidate and copy every candidate ID exactly. "
        "Use selected only for safe unattended use, ambiguous for a genuine unresolved choice, "
        "and rejected otherwise. Keep each reason short and candidate-specific."
    )


def _enforce_explicit_column_labels(
    question: str,
    candidates: tuple[NodeCandidate, ...],
    selections: tuple[NodeSelection, ...],
    *,
    candidate_nodes: Mapping[str, GraphNode],
) -> tuple[NodeSelection, ...]:
    """Prefer maximal verbatim schema labels over nested shorter interpretations."""

    explicit = {
        candidate.candidate_id: normalize_text(candidate_nodes[candidate.node_id].name)
        for candidate in candidates
        if "explicit_question_column" in candidate.channels and candidate.node_id in candidate_nodes
    }
    if not explicit:
        return selections
    question_text = normalize_text(question)
    explicit_labels = tuple(explicit.values())
    nested = set()
    for candidate in candidates:
        node = candidate_nodes.get(candidate.node_id)
        if node is None or candidate.candidate_id in explicit:
            continue
        label = normalize_text(node.name)
        if any(
            label
            and label != explicit_label
            and label in explicit_label
            and label not in question_text.replace(explicit_label, "")
            for explicit_label in explicit_labels
        ):
            nested.add(candidate.candidate_id)
    return tuple(
        NodeSelection(
            candidate_id=item.candidate_id,
            status="selected",
            role=item.role,
            reason="selected by maximal verbatim physical column label",
        )
        if item.candidate_id in explicit
        else NodeSelection(
            candidate_id=item.candidate_id,
            status="rejected",
            role=item.role,
            reason="rejected as a nested shorter interpretation of an explicit column label",
        )
        if item.candidate_id in nested
        else item
        for item in selections
    )


_MONTH_GRAIN_INTENT_RE = re.compile(
    r"(?:各月|每月|逐月|按月|分月|月度(?:明细|趋势|分布)|以月为单位|按月份)"
)


def _enforce_explicit_month_grain(
    question: str,
    candidates: tuple[NodeCandidate, ...],
    selections: tuple[NodeSelection, ...],
    *,
    candidate_nodes: Mapping[str, GraphNode],
) -> tuple[NodeSelection, ...]:
    """Do not turn a month-bearing date range into an implicit monthly GROUP BY."""

    if _MONTH_GRAIN_INTENT_RE.search(question):
        return selections
    month_grain_ids = {
        candidate.candidate_id
        for candidate in candidates
        if _is_month_grain_candidate(candidate_nodes.get(candidate.node_id))
    }
    return tuple(
        NodeSelection(
            candidate_id=item.candidate_id,
            status="rejected",
            role=item.role,
            reason="rejected because the Question has a time range but no explicit monthly grain",
        )
        if item.candidate_id in month_grain_ids and item.status != "rejected"
        else item
        for item in selections
    )


def _is_month_grain_candidate(node: GraphNode | None) -> bool:
    if node is None:
        return False
    if node.node_type == "concept":
        targets = node.properties.get("targets")
        if not isinstance(targets, list):
            return False
        expressions = [
            str(target.get("sql_expression") or "").casefold()
            for target in targets
            if isinstance(target, dict)
        ]
        return any(
            "date_format(" in expression and "%y-%m" in expression for expression in expressions
        )
    if node.node_type == "generation_rule":
        condition = str(node.properties.get("condition") or "").casefold()
        return "group by" in condition and ("月份" in condition or "按月" in condition)
    return False


def _seed_canonical_intent_rules(
    candidates: tuple[NodeCandidate, ...],
    selections: tuple[NodeSelection, ...],
) -> tuple[NodeSelection, ...]:
    """Keep deterministic intent-trigger matches from being dropped by joint review."""

    canonical_rule_ids = {
        candidate.candidate_id
        for candidate in candidates
        if candidate.node_type == "generation_rule"
        and "question_rule_trigger" in candidate.channels
        and any(
            evidence.channel == "question_rule_trigger" and evidence.detail == "top_n"
            for evidence in candidate.evidence
        )
    }
    if not canonical_rule_ids:
        return selections
    return tuple(
        NodeSelection(
            candidate_id=item.candidate_id,
            status="selected",
            role=item.role,
            reason="selected by deterministic canonical intent trigger",
        )
        if item.candidate_id in canonical_rule_ids and item.status != "selected"
        else item
        for item in selections
    )


def _selection_output_model(candidates: tuple[NodeCandidate, ...]) -> type[BaseModel]:
    """Bind the existing structured-output repair to this recall set's exact IDs."""

    candidate_id = Enum(
        "CandidateId",
        {f"candidate_{index}": item.candidate_id for index, item in enumerate(candidates)},
        type=str,
    )
    candidate_decision = create_model(
        "BoundCandidateDecision",
        __base__=CandidateDecision,
        candidate_id=(candidate_id, ...),
    )
    return create_model(
        "BoundSelectionOutput",
        __base__=SelectionOutput,
        decisions=(
            tuple[candidate_decision, ...],
            Field(
                min_length=len(candidates),
                max_length=len(candidates),
                description="Exactly one decision for every supplied candidate.",
            ),
        ),
    )


def _decision_candidate_id(decision: CandidateDecision) -> str:
    candidate_id = decision.candidate_id
    return candidate_id.value if isinstance(candidate_id, Enum) else str(candidate_id)


def _seed_unique_exact_semantic_candidates(
    candidates: tuple[NodeCandidate, ...],
    selections: tuple[NodeSelection, ...],
    *,
    candidate_nodes: Mapping[str, GraphNode],
) -> tuple[NodeSelection, ...]:
    """Select one exact executable semantic interpretation of a Question mention."""

    exact_by_mention: dict[str, list[NodeCandidate]] = {}
    for candidate in candidates:
        node = candidate_nodes.get(candidate.node_id)
        if (
            candidate.node_type not in {"concept", "metric"}
            or not _is_exact_semantic_mention(candidate)
            or not candidate.mention_ids
            or not _has_executable_semantic_contract(node)
        ):
            continue
        for mention_id in candidate.mention_ids:
            exact_by_mention.setdefault(mention_id, []).append(candidate)

    seeded = {matches[0].candidate_id for matches in exact_by_mention.values() if len(matches) == 1}
    if not seeded:
        return selections
    selection_by_id = {item.candidate_id: item for item in selections}
    return tuple(
        NodeSelection(
            candidate_id=item.candidate_id,
            status="selected",
            role=_candidate_role(item),
            reason="selected by unique exact executable semantic match",
        )
        if item.candidate_id in seeded and selection_by_id[item.candidate_id].status != "selected"
        else selection_by_id[item.candidate_id]
        for item in candidates
    )


def _is_exact_semantic_mention(candidate: NodeCandidate) -> bool:
    """Recognize the GraphIndex's exact-name and exact-alias score contracts."""

    return ("mention_name" in candidate.channels and candidate.score == 1.0) or (
        "mention_alias" in candidate.channels and candidate.score == 0.97
    )


def _has_executable_semantic_contract(node: GraphNode | None) -> bool:
    if node is None:
        return False
    if node.node_type == "concept":
        targets = node.properties.get("targets")
        return (
            isinstance(targets, list)
            and bool(targets)
            and all(
                isinstance(target, dict) and bool(str(target.get("sql_expression") or "").strip())
                for target in targets
            )
        )
    if node.node_type == "metric":
        expression = node.properties.get("expression")
        plans = node.properties.get("sql_generation_plans")
        return (
            isinstance(expression, dict)
            and bool(str(expression.get("expr") or "").strip())
            and isinstance(expression.get("refs"), dict)
            and bool(expression["refs"])
            and isinstance(plans, list)
            and bool(plans)
        )
    return False


def _enforce_complete_literal_binding(
    question: str,
    initial_sql: str | None,
    candidates: tuple[NodeCandidate, ...],
    selections: tuple[NodeSelection, ...],
    *,
    candidate_nodes: Mapping[str, GraphNode],
) -> tuple[NodeSelection, ...]:
    """Prevent a nested value recall from replacing a complete literal on another column."""

    bindings = _initial_sql_literal_bindings(initial_sql)
    if not bindings:
        return selections
    selection_by_id = {item.candidate_id: item for item in selections}
    demoted: set[str] = set()
    for candidate in candidates:
        if (
            candidate.node_type != "dimension_value"
            or selection_by_id[candidate.candidate_id].status != "selected"
        ):
            continue
        node = candidate_nodes.get(candidate.node_id)
        value = str(node.properties.get("value") or node.name).strip() if node else ""
        column_ref = str(node.properties.get("column_ref") or "") if node else ""
        candidate_column = column_ref.rsplit(".", 1)[-1]
        if not value or not candidate_column:
            continue
        for bound_column, complete_literal in bindings:
            if (
                value != complete_literal
                and value in complete_literal
                and complete_literal in question
                and candidate_column != bound_column
                and not _occurs_outside_literal(question, value, complete_literal)
            ):
                demoted.add(candidate.candidate_id)
                break
    return tuple(
        NodeSelection(
            candidate_id=item.candidate_id,
            status="rejected",
            role=item.role,
            reason="nested value cannot replace complete literal binding on another column",
        )
        if item.candidate_id in demoted
        else item
        for item in selections
    )


def _initial_sql_literal_bindings(initial_sql: str | None) -> tuple[tuple[str, str], ...]:
    if not initial_sql:
        return ()
    try:
        tree = sqlglot.parse_one(initial_sql)
    except Exception:  # noqa: BLE001 - an invalid hypothesis supplies no binding evidence
        return ()
    bindings = []
    for equality in tree.find_all(expressions.EQ):
        left, right = equality.this, equality.expression
        if isinstance(left, expressions.Literal) and isinstance(right, expressions.Column):
            left, right = right, left
        if (
            isinstance(left, expressions.Column)
            and isinstance(right, expressions.Literal)
            and right.is_string
        ):
            bindings.append((left.name, str(right.this)))
    return tuple(bindings)


def _occurs_outside_literal(question: str, value: str, complete_literal: str) -> bool:
    remainder = question.replace(complete_literal, "")
    return value in remainder


def _prefer_matching_concepts(
    candidates: tuple[NodeCandidate, ...],
    selections: tuple[NodeSelection, ...],
    *,
    concept_value_ids: Mapping[str, frozenset[str]],
) -> tuple[NodeSelection, ...]:
    """Prefer a matching Concept seed over one of its selected DimensionValue children."""

    selection_by_id = {item.candidate_id: item for item in selections}
    concepts = tuple(item for item in candidates if item.node_type == "concept")
    promoted: set[str] = set()
    demoted: set[str] = set()

    for value in candidates:
        selection = selection_by_id[value.candidate_id]
        if value.node_type != "dimension_value" or selection.status != "selected":
            continue
        eligible = [
            concept
            for concept in concepts
            if value.node_id in concept_value_ids.get(concept.node_id, frozenset())
            and bool(set(concept.mention_ids) & set(value.mention_ids))
            and concept.score >= value.score
        ]
        if not eligible:
            continue
        preferred = min(eligible, key=lambda item: (-item.score, item.node_id))
        promoted.add(preferred.candidate_id)
        demoted.add(value.candidate_id)

    if not promoted:
        return selections
    return tuple(
        NodeSelection(
            candidate_id=item.candidate_id,
            status="selected",
            role="direct",
            reason="selected by concept-over-dimension-value priority",
        )
        if item.candidate_id in promoted
        else NodeSelection(
            candidate_id=item.candidate_id,
            status="rejected",
            role="value",
            reason="supplied by selected Concept dependency closure",
        )
        if item.candidate_id in demoted
        else selection_by_id[item.candidate_id]
        for item in candidates
    )


def _enforce_concept_owned_mentions(
    candidates: tuple[NodeCandidate, ...],
    selections: tuple[NodeSelection, ...],
    *,
    candidate_nodes: Mapping[str, GraphNode],
) -> tuple[NodeSelection, ...]:
    """Exclude raw alternatives after an exact selected Concept owns their mentions."""

    selection_by_id = {item.candidate_id: item for item in selections}
    owners = [
        candidate
        for candidate in candidates
        if candidate.node_type == "concept"
        and selection_by_id[candidate.candidate_id].status == "selected"
        and candidate.score == 1.0
        and "mention_name" in candidate.channels
        and candidate.mention_ids
        and candidate.node_id in candidate_nodes
    ]
    if not owners:
        return selections

    owners_by_mention: dict[str, list[NodeCandidate]] = {}
    for owner in owners:
        for mention_id in owner.mention_ids:
            owners_by_mention.setdefault(mention_id, []).append(owner)

    demoted: set[str] = set()
    for candidate in candidates:
        if (
            candidate.node_type != "column"
            or selection_by_id[candidate.candidate_id].status != "selected"
            or not candidate.mention_ids
            or not set(candidate.mention_ids) <= set(owners_by_mention)
        ):
            continue
        applicable_owners = {
            owner.node_id: owner
            for mention_id in candidate.mention_ids
            for owner in owners_by_mention[mention_id]
        }
        owned_columns = {
            column_id
            for owner in applicable_owners.values()
            for column_id in _concept_column_ids(candidate_nodes[owner.node_id])
        }
        if candidate.node_id not in owned_columns:
            demoted.add(candidate.candidate_id)

    if not demoted:
        return selections
    return tuple(
        NodeSelection(
            candidate_id=item.candidate_id,
            status="rejected",
            role="schema",
            reason="excluded by selected Concept mention ownership",
        )
        if item.candidate_id in demoted
        else selection_by_id[item.candidate_id]
        for item in candidates
    )


def _enforce_concept_literal_column_binding(
    initial_sql: str | None,
    candidates: tuple[NodeCandidate, ...],
    selections: tuple[NodeSelection, ...],
    *,
    candidate_nodes: Mapping[str, GraphNode],
) -> tuple[NodeSelection, ...]:
    """Reject raw same-name Column alternatives after a selected Concept owns the literal source."""

    selection_by_id = {item.candidate_id: item for item in selections}
    initial_literal_bindings = {
        (column.casefold(), literal.casefold())
        for column, literal in _initial_sql_literal_bindings(initial_sql)
    }
    selected_bindings: list[tuple[NodeCandidate, str, str, str]] = []
    for candidate in candidates:
        if (
            candidate.node_type != "concept"
            or selection_by_id[candidate.candidate_id].status != "selected"
        ):
            continue
        node = candidate_nodes.get(candidate.node_id)
        if node is None:
            continue
        for column_ref, column_name, literal in _concept_literal_column_bindings(node):
            selected_bindings.append((candidate, column_ref, column_name, literal))
    if not selected_bindings:
        return selections

    demoted: set[str] = set()
    for candidate in candidates:
        if (
            candidate.node_type != "column"
            or selection_by_id[candidate.candidate_id].status != "selected"
        ):
            continue
        candidate_column = _column_name(candidate, candidate_nodes.get(candidate.node_id))
        if not candidate_column:
            continue
        for owner, column_ref, column_name, literal in selected_bindings:
            if (
                candidate.node_id == column_ref
                or candidate_column.casefold() != column_name.casefold()
            ):
                continue
            mention_overlap = bool(set(candidate.mention_ids) & set(owner.mention_ids))
            owner_literals = _concept_surface_literals(owner, candidate_nodes.get(owner.node_id))
            owner_literals.add(literal.casefold())
            initial_column_literals = {
                value
                for column, value in initial_literal_bindings
                if column == candidate_column.casefold()
            }
            literal_overlap = (
                any(
                    (candidate_column.casefold(), owner_literal) in initial_literal_bindings
                    for owner_literal in owner_literals
                )
                or any(
                    _literal_matches_surface(value, owner_literal)
                    for value in initial_column_literals
                    for owner_literal in owner_literals
                )
                or any(
                    _candidate_mentions_literal(candidate, owner_literal)
                    for owner_literal in owner_literals
                )
            )
            if mention_overlap or literal_overlap:
                demoted.add(candidate.candidate_id)
                break

    if not demoted:
        return selections
    return tuple(
        NodeSelection(
            candidate_id=item.candidate_id,
            status="rejected",
            role="schema",
            reason="excluded by selected Concept literal-column binding",
        )
        if item.candidate_id in demoted
        else selection_by_id[item.candidate_id]
        for item in candidates
    )


def _concept_column_ids(node: GraphNode) -> set[str]:
    result: set[str] = set()
    targets = node.properties.get("targets")
    if not isinstance(targets, list):
        return result
    for target in targets:
        if not isinstance(target, dict):
            continue
        references = target.get("references")
        if isinstance(references, list):
            result.update(
                reference
                for reference in references
                if isinstance(reference, str) and reference.startswith("column.")
            )
        semantic_targets = target.get("semantic_targets")
        if isinstance(semantic_targets, list):
            result.update(
                str(endpoint.get("node_id"))
                for endpoint in semantic_targets
                if isinstance(endpoint, dict)
                and endpoint.get("relation") == "maps_to_column"
                and str(endpoint.get("node_id") or "").startswith("column.")
            )
    return result


def _concept_literal_column_bindings(node: GraphNode) -> tuple[tuple[str, str, str], ...]:
    targets = node.properties.get("targets")
    if not isinstance(targets, list):
        return ()
    bindings: list[tuple[str, str, str]] = []
    for target in targets:
        if not isinstance(target, dict):
            continue
        references = target.get("references")
        column_refs = (
            {
                reference
                for reference in references
                if isinstance(reference, str) and reference.startswith("column.")
            }
            if isinstance(references, list)
            else set()
        )
        expression = str(target.get("sql_expression") or "").strip()
        for column_ref, literal in _literal_predicates_from_expression(expression):
            if not column_refs or column_ref in column_refs:
                bindings.append((column_ref, column_ref.rsplit(".", 1)[-1], literal))
    return tuple(bindings)


def _literal_predicates_from_expression(expression: str) -> tuple[tuple[str, str], ...]:
    if not expression:
        return ()
    try:
        tree = sqlglot.parse_one(expression)
    except Exception:  # noqa: BLE001 - invalid local expression supplies no deterministic binding
        return ()
    bindings: list[tuple[str, str]] = []
    expressions_to_check = [tree, *tree.find_all(expressions.Expression)]
    for item in expressions_to_check:
        if isinstance(item, expressions.EQ):
            left, right = item.this, item.expression
            if isinstance(left, expressions.Literal) and isinstance(right, expressions.Column):
                left, right = right, left
            if (
                isinstance(left, expressions.Column)
                and isinstance(right, expressions.Literal)
                and right.is_string
                and left.table
            ):
                bindings.append((f"column.{left.table}.{left.name}", str(right.this)))
        if isinstance(item, expressions.In) and isinstance(item.this, expressions.Column):
            column = item.this
            if not column.table:
                continue
            for literal in item.expressions:
                if isinstance(literal, expressions.Literal) and literal.is_string:
                    bindings.append((f"column.{column.table}.{column.name}", str(literal.this)))
    return tuple(dict.fromkeys(bindings))


def _column_name(candidate: NodeCandidate, node: GraphNode | None) -> str:
    if node is not None:
        name = str(node.properties.get("column_name") or "").strip()
        if name:
            return name
    return candidate.node_id.rsplit(".", 1)[-1]


def _concept_surface_literals(candidate: NodeCandidate, node: GraphNode | None) -> set[str]:
    aliases = node.aliases if node is not None else ()
    return {candidate.name.casefold(), *(alias.casefold() for alias in aliases)}


def _literal_matches_surface(value: str, surface: str) -> bool:
    if len(value) < 4:
        return False
    if value == surface:
        return True
    return re.search(rf"(?<!\w){re.escape(value)}(?!\w)", surface) is not None


def _candidate_mentions_literal(candidate: NodeCandidate, literal: str) -> bool:
    literal_key = literal.casefold()
    return any(literal_key in evidence.detail.casefold() for evidence in candidate.evidence)


def _candidate_role(candidate: NodeCandidate) -> str:
    if candidate.node_type == "generation_rule":
        return "rule"
    if candidate.node_type in {"table", "column"}:
        return "schema"
    if candidate.node_type == "dimension_value":
        return "value"
    return "direct"


def _selection_contract(node: GraphNode | None) -> dict[str, object]:
    """Project only the fields needed to judge whether an ontology asset applies."""

    if node is None:
        return {}
    contract: dict[str, object] = {}
    if node.aliases:
        contract["aliases"] = node.aliases
    if node.description:
        contract["description"] = node.description

    keys_by_type = {
        "column": ("column_name", "is_available_time", "is_default_time"),
        "concept": ("mapping_type", "targets"),
        "metric": (
            "references",
            "metric_kind",
            "pattern",
            "expression",
            "aggregation_policy",
            "calculation_policy",
            "sql_generation_plans",
        ),
        "generation_rule": (
            "condition",
            "action",
            "reason",
            "references",
            "triggers",
        ),
        "dimension_value": ("value", "column_ref"),
    }
    for key in keys_by_type.get(node.node_type, ()):
        value = node.properties.get(key)
        if value not in (None, "", [], {}):
            contract[key] = value
    return contract


__all__ = ["CandidateSelector"]
