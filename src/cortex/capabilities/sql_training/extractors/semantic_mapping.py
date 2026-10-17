from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from cortex.capabilities.sql_training.contracts.references import physical_reference_catalog
from cortex.capabilities.sql_training.contracts.semantic_mapping import (
    normalize_semantic_mapping_candidate,
    validate_semantic_mapping_candidate_contract,
    validate_semantic_mapping_gold_grounding,
    validate_semantic_mapping_schema_grounding,
)
from cortex.capabilities.sql_training.core.enums import NodeType
from cortex.capabilities.sql_training.core.ids import make_node_id
from cortex.capabilities.sql_training.extractors.common import (
    CandidateSource,
    TypedCandidateExtractor,
    ensure_list,
    normalize_properties_payload,
)
from cortex.capabilities.sql_training.llm.parsing import run_llm_extraction
from cortex.capabilities.sql_training.models import NodeAsset

_SEMANTIC_MAPPING_BIZ_KEYS = {
    "targets",
    "mapping_type",
}


class SemanticMappingTargetPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(
        pattern=r"^[A-Za-z][A-Za-z0-9_]{0,99}$",
        description=(
            "Stable local ID for this physical realization, such as sales_amount_target. It is "
            "not an ontology Node ID and must not depend on output order or a case identifier."
        ),
    )
    sql_expression: str = Field(
        min_length=1,
        description=(
            "Direct physical binding using only catalog-qualified table.column tokens. A field "
            "meaning uses the qualified Column; a governed value uses its full predicate. Never "
            "use query/CTE aliases or 1 = 1. Use IS NULL or IS NOT NULL only when missing or "
            "present values define the named meaning."
        ),
    )
    references: list[str] = Field(
        min_length=1,
        description=(
            "Catalog IDs used by sql_expression. Include every used Column. Include a governed "
            "Dimension Value only when its exact ID is supplied; otherwise omit it and code "
            "derives the canonical value ID from the predicate. Do not invent shortened IDs."
        ),
    )
    binding_operator: Literal["exact", "one_of", "all_of"] = Field(
        default="exact",
        description=(
            "Combination of semantic endpoints after excluding supporting owner Columns: exact "
            "for one endpoint, all_of only when no endpoint subset implements an independently "
            "named Question qualifier, and one_of for alternatives."
        ),
    )


class SemanticMappingCandidatePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_mention: str = Field(
        default="",
        description=(
            "The smallest exact continuous Question phrase containing the lexical evidence for "
            "this candidate. Several Concepts split from one compound qualifier may cite the same "
            "phrase; each name then uses only the words belonging to its own Gold binding. Do not "
            "combine separate phrases joined by and/or. Fill this field for every candidate."
        ),
    )
    entity_mention: str = Field(
        default="",
        description=(
            "The exact Question entity completed by source_mention, only when source_mention is a "
            "modifier that cannot name a Concept alone. Otherwise return an empty string."
        ),
    )
    description: str = Field(
        default="",
        description=(
            "A concise explanation of only the Concept meaning, written in the Question's "
            "primary language. Do not describe aggregation, grouping, ranking, projection, or "
            "current-query use."
        ),
    )
    name: str = Field(
        min_length=1,
        description=(
            "The shortest self-contained Question-grounded name for one atomic Concept. Use "
            "source_mention alone, or select the binding-specific words from a compound mention "
            "and combine them with entity_mention. Small grammar changes are allowed. Use only "
            "Question wording and do not introduce new business meaning. The name must "
            "identify the entity or value when read alone and be a complete noun phrase, field "
            "term, or value term. An adjective, participle, or qualifier with no head entity is "
            "invalid and must include the Question entity it modifies. Do not join independently "
            "meaningful Question phrases with and/or."
        ),
    )
    targets: list[SemanticMappingTargetPayload] = Field(
        min_length=1,
        description=(
            "Every distinct source-local Gold SQL realization of the complete name. Each target "
            "must independently realize the whole name; formula operands are not alternative "
            "targets. One realization may require several columns and predicates together."
        ),
    )


class SemanticMappingExtractionOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    assets: list[SemanticMappingCandidatePayload] = Field(
        default_factory=list,
        description=(
            "Atomic Concept candidates returned by this invocation. Return [] when this invocation "
            "finds no qualifying Question term with a direct stored-value or predicate binding. "
            "One candidate represents one term. Return coordinated independent terms as separate "
            "candidates, even when they share the same target; do not select one canonical term or "
            "omit the other during Extract. A continuous phrase may still contain several "
            "independently meaningful qualifiers with separate Gold bindings; emit one candidate "
            "for each."
        ),
    )


def extract_semantic_mapping_assets(
    llm: Any,
    record: dict[str, Any],
    prompt: str,
    *,
    trace_capture: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    question = str(record.get("question") or "")
    table_schema = record.get("database_context") or {}
    prior_case_concept_ids = {
        str(item.get("id") or "")
        for item in record.get("prior_case_concept_candidates", [])
        if isinstance(item, dict)
    }
    human_payload = {
        "question": question,
        "evidence": record.get("evidence"),
        "sql": str(record.get("sql") or ""),
        "database_dialect": str(record.get("database_dialect") or ""),
        "gold_column_metadata": record.get("gold_column_metadata") or {},
        "database_context": table_schema,
        "physical_reference_catalog": sorted(physical_reference_catalog(table_schema)),
    }
    if record.get("prior_case_concept_candidates"):
        human_payload["prior_case_concept_candidates"] = list(
            record["prior_case_concept_candidates"]
        )
    if record.get("extraction_self_check"):
        human_payload["extraction_self_check"] = record["extraction_self_check"]
    errors: list[str] = []
    previous_candidates: list[dict[str, Any]] = []
    for attempt in range(1, 4):
        request = dict(human_payload)
        if errors:
            request["previous_candidate_assets"] = previous_candidates
            request["validation_feedback"] = {
                "error": errors[-1],
                "instruction": _semantic_mapping_retry_instruction(errors[-1]),
            }
        candidates = run_llm_extraction(
            llm=llm,
            prompt=prompt,
            output_model=SemanticMappingExtractionOutput,
            human_payload=request,
            map_result=lambda parsed: [
                _semantic_candidate_to_raw_candidate(item) for item in _payload_assets(parsed)
            ],
            trace_capture=trace_capture if attempt == 1 else None,
        )
        previous_candidates = candidates
        candidate_errors: list[str] = []
        normalized_candidates: list[dict[str, Any]] = []
        for candidate in candidates:
            name = str(candidate.get("name") or "")
            try:
                candidate.pop("_source_mention", "")
                normalized_payload, properties = normalize_properties_payload(
                    candidate,
                    property_keys=_SEMANTIC_MAPPING_BIZ_KEYS,
                )
                normalized_payload["properties"] = properties
                asset = normalize_semantic_mapping_candidate(
                    NodeAsset.model_validate(normalized_payload),
                    table_schema=table_schema if isinstance(table_schema, dict) else {},
                )
                validate_semantic_mapping_candidate_contract(
                    asset,
                    table_schema=table_schema if isinstance(table_schema, dict) else {},
                )
                validate_semantic_mapping_schema_grounding(
                    asset,
                    table_schema=table_schema if isinstance(table_schema, dict) else {},
                )
                validate_semantic_mapping_gold_grounding(
                    asset,
                    gold_physical_refs=(
                        set(record["gold_physical_refs"])
                        if "gold_physical_refs" in record
                        else None
                    ),
                )
                repeated_slot_errors = _repeated_compound_slot_errors(asset)
                if repeated_slot_errors:
                    raise ValueError("; ".join(repeated_slot_errors))
                if asset.id in prior_case_concept_ids:
                    raise ValueError(
                        "SECOND_PASS_REPEATED_CONCEPT: omission self-check must not repeat or "
                        "revise a first-pass Concept"
                    )
                normalized_candidates.append(
                    asset.model_dump(mode="json", exclude={"sources"})
                )
            except ValueError as exc:
                candidate_errors.append(f"{candidate.get('id', name)}: {exc}")
        if not candidate_errors:
            if trace_capture is not None:
                trace_capture["construction_attempt"] = attempt
                trace_capture["construction_errors"] = errors
            return normalized_candidates
        errors.append("; ".join(candidate_errors))
        if attempt >= 2 and normalized_candidates:
            if trace_capture is not None:
                trace_capture["construction_attempt"] = attempt
                trace_capture["construction_errors"] = errors
                trace_capture["discarded_candidate_errors"] = candidate_errors
            return normalized_candidates
    raise ValueError(f"Concept construction failed after 3 attempts: {errors[-1]}")


def _semantic_mapping_retry_instruction(error: str) -> str:
    base = (
        "Fix the reported error and return the complete corrected Mapping set. Preserve every "
        "candidate and target from previous_candidate_assets that is not implicated by "
        "validation_feedback; correct or omit only the implicated candidates. Every target must "
        "directly return its complete name at the intrinsic semantic grain. "
    )
    corrections: list[str] = []
    if "must reference a qualified column" in error or "unqualified columns" in error:
        corrections.append(
            "A physical binding uses catalog-qualified table.column tokens. If the mention only "
            "means all/every/全部/所有/各/每个 records with no additional Gold filter, omit that "
            "Mapping; do not invent 1 = 1 or column IS NOT NULL without Question, evidence, or "
            "metadata support."
        )
    if "references do not match its physical contract" in error:
        corrections.append(
            "Include every qualified Column used by sql_expression. Use a Dimension Value only "
            "when its full ID is known; otherwise omit it because code derives it from the "
            "predicate. Never invent a shortened Dimension Value ID."
        )
    if "multiple Semantic Mapping endpoints" in error:
        corrections.append(
            "Recheck atomicity and ownership before choosing binding_operator. Split independently "
            "meaningful qualifiers. Omit a Concept whose endpoints implement joins, existence, "
            "non-existence, set difference, or other query coordination; leave that behavior to "
            "Generation Rule. Only for one established atomic stored meaning use exact for one "
            "endpoint, all_of for jointly required endpoints, or one_of for alternatives."
        )
    if "requires a physical semantic endpoint" in error:
        corrections.append(
            "A Concept semantic endpoint must be a physical Column or governed Dimension Value. "
            "A Table identifies a source only and is never a Concept endpoint. If Gold proves only "
            "a table-level population or COUNT(*) without directly realizing the entity through "
            "a Column or value endpoint, omit that Concept."
        )
    if "query or calculation operations" in error:
        corrections.append(
            "Remove aggregation, arithmetic measure construction, ranking, subqueries, and other "
            "query operations. Emit only independently grounded atomic Concepts; leave intrinsic "
            "named calculations to Metric and query application to Generation Rule."
        )
    if "not directly realized in Gold SQL" in error:
        corrections.append(
            "Schema availability is not evidence. Omit any Mapping whose target Column does not "
            "participate in Gold SQL lineage that directly realizes the named mention."
        )
    if "spans repeated compound slots" in error:
        corrections.append(
            "A repeated compound value needs one complete target per numbered slot. Pair only "
            "the components with the same slot number; never combine several slots in one target."
        )
    if not corrections:
        corrections.append(
            "Use only canonical target fields and make references and binding_operator agree "
            "with the target's complete physical expression."
        )
    return base + " ".join(corrections)


def _repeated_compound_slot_errors(asset: NodeAsset) -> list[str]:
    """Reject one target that collapses several numbered compound-value slots."""

    errors: list[str] = []
    targets = getattr(asset.properties, "targets", ())
    for target in targets:
        by_slot: dict[str, set[str]] = {}
        for reference in target.references:
            if not reference.startswith("column."):
                continue
            column = reference.rsplit(".", 1)[-1]
            match = re.fullmatch(r"(.+?)(\d+)", column)
            if match is None:
                continue
            stem, slot = match.groups()
            by_slot.setdefault(slot, set()).add(stem.casefold())
        compound_slots = {slot for slot, stems in by_slot.items() if len(stems) >= 2}
        if len(compound_slots) >= 2:
            errors.append(
                f"Semantic Mapping target {target.id!r} spans repeated compound slots "
                f"{sorted(compound_slots)}"
            )
    return errors


def _payload_assets(payload: Any) -> list[SemanticMappingCandidatePayload]:
    if isinstance(payload, SemanticMappingExtractionOutput):
        return list(payload.assets)
    raw_assets = payload.get("assets", []) if isinstance(payload, dict) else []
    if not isinstance(raw_assets, list):
        raw_assets = [raw_assets]
    validated: list[SemanticMappingCandidatePayload] = []
    for item in raw_assets:
        try:
            validated.append(SemanticMappingCandidatePayload.model_validate(item))
        except Exception:
            continue
    return validated


def _semantic_candidate_to_raw_candidate(
    item: SemanticMappingCandidatePayload,
) -> dict[str, Any]:
    return {
        "node_type": NodeType.SEMANTIC_MAPPING.value,
        "id": make_node_id(NodeType.SEMANTIC_MAPPING, item.name),
        "name": item.name,
        "aliases": [],
        "description": item.description,
        "tags": [],
        "_source_mention": item.source_mention,
        "targets": [
            {
                "id": target.id,
                "sql_expression": target.sql_expression,
                "references": list(target.references),
                "binding_operator": target.binding_operator,
            }
            for target in item.targets
        ],
    }


class SemanticMappingExtractor(TypedCandidateExtractor):
    asset_type = NodeType.SEMANTIC_MAPPING

    def __init__(self, *, table_schema: dict[str, str] | None = None):
        self.table_schema = table_schema or {}

    def extract(
        self,
        raw_assets: list[dict[str, Any]],
        *,
        source: CandidateSource,
        skipped: list[dict[str, Any]],
    ) -> list[NodeAsset]:
        assets = super().extract(
            raw_assets,
            source=source,
            skipped=skipped,
        )
        valid_assets = []
        for asset in assets:
            try:
                asset = normalize_semantic_mapping_candidate(
                    asset,
                    table_schema=self.table_schema,
                )
                validate_semantic_mapping_candidate_contract(
                    asset,
                    table_schema=self.table_schema,
                )
                validate_semantic_mapping_schema_grounding(
                    asset,
                    table_schema=self.table_schema,
                )
            except ValueError as exc:
                skipped.append(
                    {
                        "code": (f"INVALID_{source.error_prefix}_SEMANTIC_MAPPING_CONTRACT"),
                        "record": source.source_id,
                        "asset_id": asset.id,
                        "error": str(exc),
                    }
                )
                continue
            valid_assets.append(asset)
        return valid_assets

    def _normalize(
        self,
        raw_asset: dict[str, Any],
        source: CandidateSource,
    ) -> dict[str, Any]:
        normalized, properties = normalize_properties_payload(
            raw_asset,
            property_keys=_SEMANTIC_MAPPING_BIZ_KEYS,
        )
        normalized["node_type"] = NodeType.SEMANTIC_MAPPING
        name = str(normalized.get("name") or "").strip()
        if name and not normalized.get("id"):
            normalized["id"] = make_node_id(
                NodeType.SEMANTIC_MAPPING,
                name,
            )
        normalized["aliases"] = [str(item) for item in ensure_list(normalized.get("aliases"))]
        normalized["tags"] = []
        normalized["properties"] = properties
        return super()._normalize(normalized, source)
