"""One-shot final-content merge, with no decomposition or semantic verifier."""

from __future__ import annotations

from pathlib import Path
from time import monotonic

from pydantic import BaseModel, ConfigDict, Field

from cortex.capabilities.graph_merge.llm.references import _RefCodec
from cortex.capabilities.graph_merge.models import (
    DifferencePoint,
    ReconciledNode,
    ReconciledRepresentation,
)
from cortex.capabilities.graph_merge.reconciliation.evidence import (
    evidence_complete,
    resolve_group_evidence,
)
from cortex.capabilities.graph_merge.reconciliation.materialization import (
    _materialize_node,
    _sources_for_refs,
)
from cortex.capabilities.graph_merge.reconciliation.materialization import (
    materialize as materialize_group,
)
from cortex.capabilities.graph_merge.reconciliation.service import NodeGroupReconciler
from cortex.infrastructure.llm.parsing import _parse_output, run_llm_extraction_sync
from cortex.schema import GraphSpace, validate_graph_space
from cortex.capabilities.retrieve_db.concept_grounding_asset_definitions import render_ontology_asset_contract

PROMPT = Path(__file__).with_name("prompts") / "direct_merge.md"
SEMANTIC = frozenset({"concept", "metric", "generation_rule"})


class DirectMergeOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    representations: tuple[ReconciledRepresentation, ...] = Field(min_length=1)
    difference_points: tuple[DifferencePoint, ...] = ()


def validate_partition(outcome, group):
    expected = {member.ref for member in group.members}
    actual = [ref for row in outcome.representations for ref in row.supporting_refs]
    if set(actual) != expected or len(actual) != len(expected):
        raise ValueError(
            "output supporting_refs must partition all input refs exactly once"
        )
    if any(not row.supporting_refs for row in outcome.representations):
        raise ValueError("empty membership or invented historical alternative ID")
    if bool(outcome.difference_points) != (len(outcome.representations) > 1):
        raise ValueError(
            "multiple representations require conflict points; one requires none"
        )
    source_ids = {
        member.ref: {s.case_id for s in member.sources if s.case_id is not None}
        for member in group.members
    }
    for point in outcome.difference_points:
        members = [m for position in point.positions for m in position.members]
        refs = [m.representation_ref for m in members]
        if point.resolution != "conflict" or len(point.positions) < 2:
            raise ValueError(
                "difference point must contain at least two conflict positions"
            )
        if set(refs) != expected or len(refs) != len(expected):
            raise ValueError("conflict positions must partition every source ref")
        if any(
            set(m.source_case_ids) != source_ids[m.representation_ref] for m in members
        ):
            raise ValueError("conflict source Case IDs differ from supplied provenance")
        positions = {
            member.representation_ref: index
            for index, position in enumerate(point.positions)
            for member in position.members
        }
        if any(
            len({positions[ref] for ref in row.supporting_refs}) != 1
            for row in outcome.representations
        ):
            raise ValueError("one output alternative spans conflicting positions")


class DirectGroupMerger(NodeGroupReconciler):
    def __init__(
        self,
        llm,
        evidence_resolver,
        trace_sink=None,
        checkpoint=None,
        available_nodes=(),
    ):
        super().__init__(
            evidence_resolver=evidence_resolver,
        )
        self.llm = llm
        self.trace_sink = trace_sink
        self.checkpoint = checkpoint
        self.records = []
        self.available_nodes = {node.id: node for node in available_nodes}

    def reconcile_group(
        self,
        group,
        *,
        schema,
        historical_conflict=None,
        dependency_nodes=(),
        **kwargs,
    ):
        if historical_conflict is not None:
            raise ValueError(
                "direct ablation requires a frozen pre-reconciliation input"
            )
        if group.node_type not in SEMANTIC or len(group.members) == 1:
            return super().reconcile_group(
                group,
                schema=schema,
                dependency_nodes=dependency_nodes,
            )
        dependencies = {node.id: node for node in dependency_nodes}
        pending = [
            ref
            for member in group.members
            for ref in _content_references(member.content.properties)
        ]
        member_ids = {member.ref.ref_id for member in group.members}
        visited = set()
        while pending:
            ref = pending.pop()
            if ref in visited or ref in member_ids:
                continue
            visited.add(ref)
            node = self.available_nodes.get(ref) or dependencies.get(ref)
            if node is not None:
                dependencies[ref] = node
                pending.extend(_content_references(node.properties))
        dependency_nodes = tuple(dependencies[key] for key in sorted(dependencies))
        evidence_by_ref = resolve_group_evidence(group, self.evidence_resolver)
        evidence_code = (
            None
            if evidence_complete(group.members, evidence_by_ref)
            else "EVIDENCE_UNAVAILABLE"
        )
        canonical = self.canonical_id_for_group(group)
        record = {
            "group_id": group.group_id,
            "members": len(group.members),
            "fallback": False,
            "llm_calls": 0,
            "semantic_verify_calls": 0,
        }
        payload = {
            "group": group.model_dump(mode="json"),
            "members_with_evidence": [
                {
                    "representation_ref": member.ref.model_dump(mode="json"),
                    "evidence": [
                        e.model_dump(mode="json") for e in evidence_by_ref[member.ref]
                    ],
                }
                for member in group.members
            ],
            "node_type": schema.node_type_index()[group.node_type].model_dump(
                mode="json"
            ),
            "effective_fields": {
                key: field.model_dump(mode="json")
                for key, field in schema.effective_node_fields(group.node_type).items()
            },
            "dependency_evidence": [
                node.model_dump(mode="json") for node in dependency_nodes
            ],
        }
        codec = _RefCodec.from_payload(payload)
        sections = ("common", "owners", group.node_type)
        prompt = PROMPT.read_text() + "\n" + render_ontology_asset_contract(sections)
        trace = {}
        started = monotonic()
        cached = self.checkpoint and self.checkpoint.has(group, payload, prompt)
        if self.trace_sink:
            print(
                f"direct_group_start {group.group_id} members={len(group.members)}",
                flush=True,
            )

        def materialize(outcome):
            if len(outcome.representations) == 1:
                row = outcome.representations[0]
                return ReconciledNode(
                    node=_materialize_node(
                        canonical,
                        group.node_type,
                        row.content,
                        _sources_for_refs(row.supporting_refs, group.members),
                    ),
                    representations=group.members,
                    evidence_by_ref=evidence_by_ref,
                )
            return materialize_group(
                self.id_factory,
                canonical,
                group,
                outcome.representations,
                outcome.difference_points,
                evidence_by_ref,
                [],
            )

        try:
            if cached:
                outcome = self.checkpoint.load(group)
                record.update(self.checkpoint.record(group))
                record["original_llm_calls"] = record.get(
                    "original_llm_calls", record["llm_calls"]
                )
                record["original_elapsed_seconds"] = record.get(
                    "original_elapsed_seconds", record.get("elapsed_seconds", 0)
                )
                record["llm_calls"] = 0
                record["resumed"] = True
                obsolete_id_check = any(
                    message in record.get("error", "")
                    for message in (
                        "Unknown NODE ID type:",
                        "Semantic Mapping target id must start with a letter",
                    )
                )
                if record.get("fallback") and obsolete_id_check:
                    original = self.checkpoint.original_response(group)
                    if original is not None:
                        outcome = codec.decode_model(
                            _parse_output(original, DirectMergeOutcome),
                            DirectMergeOutcome,
                        )
                        record["previous_structural_error"] = record.pop("error")
                        record["fallback"] = False
                        record["structural_revalidation"] = True
            else:
                record["llm_calls"] = 1
                proposal = run_llm_extraction_sync(
                    llm=self.llm,
                    prompt=prompt,
                    output_model=DirectMergeOutcome,
                    human_payload=codec.encode(payload),
                    trace_capture=trace,
                    allow_repair=False,
                )
                outcome = codec.decode_model(proposal, DirectMergeOutcome)
            if not record["fallback"]:
                validate_partition(outcome, group)
            for row in outcome.representations:
                node = _materialize_node(canonical, group.node_type, row.content, ())
                validate_graph_space(GraphSpace(nodes=(node,)), schema)
                for field in ("targets", "sql_generation_plans"):
                    ids = [
                        item.get("id") for item in row.content.properties.get(field, ())
                    ]
                    if len(ids) != len(set(ids)):
                        raise ValueError(f"duplicate {field} IDs in model output")
                known = {m.ref.ref_id for m in group.members} | {
                    n.id for n in dependency_nodes
                }
                # Public Graph IDs are arbitrary strings; the train-only NodeAsset
                # ID prefix contract must not reject collision-safe incoming IDs.
                unknown = _content_references(row.content.properties) - known
                if unknown:
                    raise ValueError(f"unknown output reference IDs: {sorted(unknown)}")
            result = materialize(outcome)
        except Exception as exc:  # noqa: BLE001 - ablation preserves originals on model failure
            record.update(fallback=True, error=f"{type(exc).__name__}: {exc}")
            outcome = DirectMergeOutcome(
                representations=tuple(
                    ReconciledRepresentation(
                        content=m.content, supporting_refs=(m.ref,)
                    )
                    for m in group.members
                )
            )
            result = materialize(outcome)
        record["elapsed_seconds"] = round(monotonic() - started, 3)
        record["output_definitions"] = len(outcome.representations)
        record["semantic_conflict_reported"] = bool(outcome.difference_points)
        if self.checkpoint and (
            not record.get("resumed") or record.get("structural_revalidation")
        ):
            self.checkpoint.save(
                group,
                payload,
                prompt,
                outcome,
                record=record,
                raw_response=trace.get("response")
                or self.checkpoint.original_response(group),
            )
        self.records.append(record)
        if self.trace_sink:
            print(
                f"direct_group_complete {group.group_id} fallback={record['fallback']} "
                f"seconds={record['elapsed_seconds']}",
                flush=True,
            )
            self.trace_sink.record(
                {
                    "component": "direct_llm_merge",
                    **record,
                    **trace,
                    "parsed_response": outcome.model_dump(mode="json"),
                }
            )
        diagnostics = tuple(code for code in (evidence_code,) if code)
        if record["fallback"]:
            diagnostics += ("MODEL_FAILURE",)
        return result.model_copy(update={"diagnostic_codes": diagnostics})


def _content_references(properties):
    refs = set(properties.get("references", ()))
    expression = properties.get("expression")
    if isinstance(expression, dict):
        refs.update(expression.get("refs", {}).values())
    for target in properties.get("targets", ()):
        refs.update(target.get("references", ()))
    for field in ("table_ref", "column_ref"):
        if properties.get(field):
            refs.add(properties[field])
    return refs
