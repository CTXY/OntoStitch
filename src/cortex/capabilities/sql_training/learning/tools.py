"""Six domain tools. No tool can alter controller facts or acceptance decisions."""

from __future__ import annotations

import json
from typing import Annotated, Any, Literal

from langchain_core.tools import tool
from pydantic import Field, ValidationError, WithJsonSchema

from cortex.application.ontology_graph import (
    GraphManager,
    GraphRejected,
    GraphResult,
    input_issues,
    issue,
)
from cortex.capabilities.sql_training.core.enums import NodeType
from cortex.capabilities.sql_training.graph.mutations import (
    ChangeGroup,
    GraphEdit,
    GraphOperation,
    apply_edit,
)
from cortex.capabilities.sql_training.graph.snapshots import (
    fingerprint,
    graph_diff,
    semantic_asset,
    snapshot_items,
)
from cortex.capabilities.sql_training.graph.values import ValueWrite, value_asset
from cortex.capabilities.sql_training.learning.database import (
    GroundingDatabase,
    investigate_database,
)
from cortex.capabilities.sql_training.learning.inputs import (
    AUTHORING_MODELS,
    conflict_operation_schema,
    edge_operation_schema,
    ontology_contract,
    relation_contract_schema,
    semantic_asset_schema,
)
from cortex.capabilities.sql_training.learning.prompt_rendering import (
    render_type_guide,
    render_write_guide,
)
from cortex.capabilities.sql_training.learning.session import (
    LearningSession,
    WorkingNotes,
)
from cortex.capabilities.sql_training.llm.generation_rule_taxonomy import (
    render_generation_rule_taxonomy,
)
from cortex.capabilities.sql_training.llm.prompts.asset_contract import (
    render_ontology_asset_contract,
)
from cortex.capabilities.sql_training.models import (
    NodeAsset,
)
from cortex.schema.relation_contracts import (
    authoring_schema,
    unregistered_relation_types,
)
from cortex.schema.relation_vocabulary import RETIRED_RELATIONS

READ_TOOLS = frozenset({"inspect_graph", "inspect_database", "read_evidence"})
WRITE_TOOLS = frozenset({"edit_graph", "update_notes", "handoff"})


def build_tools(session: LearningSession):
    @tool
    def inspect_graph(
        mode: Literal[
            "nodes", "node", "dependencies", "edges", "diff", "guide", "relations"
        ] = "nodes",
        node_id: str = "",
        revision: str = "",
        node_type: str = "",
        edge_type: str = "",
        offset: Annotated[int, Field(ge=0)] = 0,
        limit: Annotated[int, Field(ge=1, le=50)] = 20,
        guide_section: Literal["schema", "contract", "taxonomy"] = "schema",
    ) -> dict:
        """Read graph nodes, full definitions, dependents, tentative diff or type guide.

        mode=guide returns one node type's instructions, example and schema; dimension_value explains verified Value proposals. generation_rule includes the operation registry for Direct and Repair, so no separate registry call is needed. mode=relations returns the edge-write guide and relation index; specify edge_type for exact fields, or node_type/node_id to scope the index. mode=edges reads stored outgoing edges. guide_section=contract/taxonomy supplies optional details. Reuse guides while in context; offset/limit page node rows.
        """
        if offset < 0 or not 1 <= limit <= 50:
            raise ValueError("offset >= 0 and 1 <= limit <= 50 required")
        snap = session.graph.read(revision or session.graph.working_revision)
        if mode == "relations":
            contract = ontology_contract(authoring_schema(session.graph.schema, snap.graph))
            owner_type = node_type
            if node_id:
                owner = next(
                    (asset for asset in snap.assets if asset.id == node_id), None
                )
                if owner is None:
                    raise ValueError("node does not exist at this revision")
                owner_type = owner.node_type.value
            relations = [
                edge
                for edge in contract["relations"]
                if not owner_type or owner_type in edge["from"]
            ]
            if edge_type:
                relations = [edge for edge in relations if edge["type"] == edge_type]
                if (
                    edge_type in authoring_schema(session.graph.schema, snap.graph).edge_type_index()
                    and not relations
                ):
                    raise ValueError(
                        "declared relation has no permitted endpoints in this scope"
                    )
            else:
                relations = [
                    {key: edge[key] for key in ("type", "from", "to", "description")}
                    for edge in relations
                ]
            if owner_type == "concept":
                relations = [{**edge, "to": [kind for kind in edge["to"] if kind != "table"]}
                             if edge["type"] == "grounded_on" else edge for edge in relations]
            total = len(relations)
            page = relations[offset : offset + limit]
            guide = render_write_guide(contract, "relations")
            # The scoped index below replaces the guide's unscoped inventory.
            guide.pop("predicates", None)
            result = {
                **guide,
                "revision": snap.revision,
                "relations": page,
                "total": total,
                "custom_relation": bool(edge_type and not relations and edge_type not in RETIRED_RELATIONS),
                **({"retired_relation": RETIRED_RELATIONS[edge_type]} if edge_type in RETIRED_RELATIONS else {}),
                "unregistered_relation_types": unregistered_relation_types(session.graph.schema, snap.graph),
                "instruction": "Specify edge_type for its complete contract. New predicates require relation_contracts in the same edit as their edges. Code manages reference_origin.",
            }
            # Page the index rather than hiding its usable schema behind an evidence ref.
            while len(page) > 1 and len(json.dumps(result, ensure_ascii=False)) > session.config.agent_tool_characters - 80:
                page.pop()
            result["next_offset"] = offset + len(page) if offset + len(page) < total else None
            return result

        if mode == "guide":
            if node_type == "dimension_value":
                return session.summarize(
                    {
                        **render_write_guide(
                            ontology_contract(authoring_schema(session.graph.schema, snap.graph)), node_type
                        ),
                        "revision": snap.revision,
                    }
                )
            if node_type and node_type not in AUTHORING_MODELS:
                raise ValueError("unknown semantic type")
            sections = (node_type,) if node_type else ("common", "owners")
            text = render_ontology_asset_contract(sections)
            if guide_section == "contract":
                return session.summarize({"contract": text, "revision": snap.revision})
            if guide_section == "taxonomy":
                return session.summarize(
                    {"taxonomy": render_generation_rule_taxonomy()}
                )
            if not node_type:
                return {
                    "available_guides": [
                        "concept",
                        "event",
                        "metric",
                        "generation_rule",
                        "dimension_value",
                    ],
                    "instruction": "Choose the node_type you plan to write; each guide includes its node example and exact schema.",
                }
            contract_ref = session.evidence.put("type_contract", {"contract": text})
            return session.summarize(
                {
                    **render_type_guide(
                        ontology_contract(authoring_schema(session.graph.schema, snap.graph)), node_type
                    ),
                    "contract_ref": contract_ref,
                    "instruction": "Author with this guide and schemas. contract_ref is optional detail for a concrete unresolved exception. Example IDs are illustrative, not case facts.",
                    "revision": snap.revision,
                }
            )
        by_id = {a.id: a for a in snap.assets}
        if mode == "node":
            manager = session.graph.manager(snap.revision)
            conflict = manager.get_conflict(node_id)
            if conflict is not None:
                result = {
                    "node": manager.get_node(node_id).model_dump(mode="json"),
                    "relations": [
                        e.model_dump(mode="json")
                        for e in manager.get_edges(node_id, direction="both")
                    ],
                }
                return session.summarize({"revision": snap.revision, "result": result})
            if node_id not in by_id:
                raise ValueError("node does not exist at this revision")
            result: Any = by_id[node_id].model_dump(mode="json")
            result["relations"] = [
                e.model_dump(mode="json")
                for e in session.graph.manager(snap.revision).get_edges(
                    node_id, direction="both"
                )
            ]
        elif mode == "dependencies":
            manager = session.graph.manager(snap.revision)
            result = {
                "references": manager.get_dependencies(node_id),
                "referenced_by": manager.get_dependents(node_id),
            }
        elif mode == "edges":
            result = [
                edge.model_dump(mode="json")
                for edge in session.graph.manager(snap.revision).get_edges(
                    node_id or None
                )
            ]
        elif mode == "diff":
            before = session.graph.accepted
            changed = graph_diff(before, snap)
            old_items = snapshot_items(before)
            old_items.update(
                {e.id: e.model_dump(mode="json") for e in before.explicit_edges}
            )
            new_items = snapshot_items(snap)
            new_items.update(
                {e.id: e.model_dump(mode="json") for e in snap.explicit_edges}
            )
            result = [
                {
                    "id": key,
                    "before": old_items.get(key),
                    "after": new_items.get(key),
                }
                for key in changed
            ]
        else:
            rows = [
                n for n in snap.graph.nodes if not node_type or n.node_type == node_type
            ]
            result = {
                "nodes": [
                    {"id": a.id, "type": a.node_type, "name": a.name}
                    for a in rows[offset : offset + limit]
                ],
                "total": len(rows),
                "next_offset": offset + limit if offset + limit < len(rows) else None,
            }
        return session.summarize({"revision": snap.revision, "result": result})

    @tool
    def inspect_database(
        mode: Literal["metadata", "sample", "exact", "statistics", "query"],
        table: str = "",
        column: str = "",
        value: str = "",
        sql: str = "",
        limit: Annotated[int, Field(ge=1, le=100)] = 10,
    ) -> dict:
        """Investigate the configured database read-only within its frozen catalog.

        Inspect metadata, bounded sample, exact categorical value, statistics, or one SELECT/CTE query. Results include a local evidence reference. Reads do not create nodes; submit governed Values through edit_graph.values or a supported Concept predicate.
        """
        return investigate_database(
            session,
            mode=mode,
            table=table,
            column=column,
            value=value,
            sql=sql,
            limit=limit,
        )

    @tool
    def read_evidence(evidence_id: str, offset: int = 0, limit: int = 8000) -> dict:
        """Read this case's saved validation, feedback, transcript or long tool result.

        Use evidence IDs supplied in context or tools; offset/limit page serialized text without injecting every historical round into working memory.
        """
        if offset < 0 or not 1 <= limit <= session.config.agent_tool_characters:
            raise ValueError("invalid evidence page")
        saved = session.evidence.read(evidence_id)
        text = json.dumps(saved["payload"], ensure_ascii=False, default=str)
        return {
            "evidence_id": evidence_id,
            "kind": saved["kind"],
            "content": text[offset : offset + limit],
            "next_offset": offset + limit if offset + limit < len(text) else None,
        }

    def _edit_graph_impl(
        edit_id: str,
        expected_revision: str,
        upserts: list[Annotated[NodeAsset, WithJsonSchema(semantic_asset_schema())]]
        | None = None,
        revert_ids: list[str] | None = None,
        proposed_groups: list[ChangeGroup] | None = None,
        edge_operations: list[dict[str, Any]] | None = None,
        conflict_operations: list[dict[str, Any]] | None = None,
        relation_contracts: list[dict[str, Any]] | None = None,
    ) -> dict:
        """Atomically save semantic definitions and explicit relations, or revert tentative IDs.

        Use the current Working revision. Reuse edit_id only for an identical retry. Upserts are full assets; asset.id determines create/update. Semantic IDs use concept.*, event.*, metric.* or generation_rule.*, matching node_type; omit sources because the service attaches provenance. Revert only tentative changes to the latest accepted content. All changes publish atomically after validation; references must remain legal, and physical nodes and existing IDs/types cannot change. proposed_groups assigns each changed node to one fixed repair target group; keep inseparable changes together. Acceptance belongs to the controller.
        """
        upserts, revert_ids, proposed_groups = (
            upserts or [],
            revert_ids or [],
            proposed_groups or [],
        )
        targets = {str(t["target_id"]) for t in session.targets}
        if session.mode == "repair" and any(
            set(g.target_ids) - targets for g in proposed_groups
        ):
            raise ValueError(
                "proposed_groups must reference this round's fixed target IDs"
            )
        before = session.graph.working
        edge_ids = {
            e.id
            for e in [*before.explicit_edges, *session.graph.accepted.explicit_edges]
        }
        edge_reverts = [key for key in revert_ids if key in edge_ids]
        node_reverts = [key for key in revert_ids if key not in edge_ids]
        request_hash = fingerprint(
            {
                "edit_id": edit_id,
                "expected_revision": expected_revision,
                "upserts": [a.model_dump(mode="json") for a in upserts],
                "revert_ids": revert_ids,
                "proposed_groups": [g.model_dump(mode="json") for g in proposed_groups],
                "edge_operations": edge_operations or [],
                "conflict_operations": conflict_operations or [],
                "relation_contracts": relation_contracts or [],
            }
        )
        previous = session.edit_receipts.get(edit_id)
        if previous is not None:
            if previous[0] != request_hash:
                raise ValueError("edit_id was already used for different content")
            cursor = session.graph.working
            while cursor.revision != previous[1] and cursor.parent_revision:
                cursor = session.graph.read(cursor.parent_revision)
            if cursor.revision != previous[1]:
                raise ValueError(
                    "edit was discarded; inspect graph and use a new edit_id"
                )
            result = cursor
        else:
            current_ids = {a.id for a in before.assets}
            edit = GraphEdit(
                edit_id=edit_id,
                expected_revision=expected_revision,
                operations=[
                    GraphOperation(
                        operation="update" if a.id in current_ids else "create",
                        node_id=a.id,
                        asset=a,
                    )
                    for a in upserts
                ]
                + [
                    GraphOperation(operation="revert", node_id=key)
                    for key in node_reverts
                ],
                proposed_groups=proposed_groups,
                edge_operations=edge_operations or [],
                conflict_operations=conflict_operations or [],
                relation_contracts=relation_contracts or [],
                revert_edge_ids=edge_reverts,
            )
            result = apply_edit(
                session.graph,
                edit,
                database=GroundingDatabase(session),
                source_id=str(session.record["source_id"]),
            )
            session.edit_receipts[edit_id] = (request_hash, result.revision)
            session.groups.extend(proposed_groups)
        return {
            "status": "ok",
            "applied_revision": result.revision,
            "working_revision": session.graph.working_revision,
            "changed_ids": graph_diff(before, session.graph.working),
        }

    @tool
    def edit_graph(
        edit_id: str,
        expected_revision: str,
        upserts: list[
            Annotated[dict[str, Any], WithJsonSchema(semantic_asset_schema())]
        ]
        | None = None,
        revert_ids: list[str] | None = None,
        proposed_groups: list[ChangeGroup] | None = None,
        edge_operations: list[
            Annotated[dict[str, Any], WithJsonSchema(edge_operation_schema())]
        ]
        | None = None,
        conflict_operations: list[
            Annotated[dict[str, Any], WithJsonSchema(conflict_operation_schema())]
        ]
        | None = None,
        relation_contracts: list[
            Annotated[dict[str, Any], WithJsonSchema(relation_contract_schema())]
        ] | None = None,
        delete_ids: list[str] | None = None,
        values: list[ValueWrite] | None = None,
    ) -> dict:
        """Atomically write semantic nodes, verified Values and explicit relations.

        Submit complete nodes from the type guide in upserts, using exact IDs and the current Working revision. Author every semantic relation through edge_operations with id, edge_type, from_node_id, to_node_id and properties.description. Targets describe matching groups and may overlap; they do not generate edges. Metric.expression.refs binds operands; include their explicit relations. Use specific predicates and descriptions for business roles and limits, not qualifier/role/condition fields. New predicates require a relation_contracts definition with endpoints, meaning, direction_semantics and evidence_requirement in the same edit. Existing contracts cannot be overwritten. Code maintains sources and branch metadata; omit reference_origin, sources and alternative_ids. conflict_operations supports set_conflict or resolve_conflict. values accepts verified column_id/value proposals, not raw Value upserts. revert_ids restores tentative edits; delete_ids removes only newly created tentative nodes. Rejected edits change nothing: follow errors[].path/hint and use a new edit_id. Reuse edit_id only for identical retries. Only the controller accepts knowledge.
        """
        try:
            parsed = []
            for index, raw in enumerate(upserts or []):
                try:
                    asset = NodeAsset.model_validate(raw)
                    if asset.node_type == NodeType.DIMENSION_VALUE:
                        raise GraphRejected(
                            [
                                issue(
                                    "OPERATION_NOT_ALLOWED",
                                    "Submit Value proposals through values.",
                                    "Code verifies the stored value and generates its node.",
                                    path=f"upserts.{index}",
                                )
                            ]
                        )
                    parsed.append(asset)
                except ValidationError as exc:
                    errors = input_issues(exc, operation_index=index, path=f"upserts.{index}")
                    # The NodeAsset pre-validator dispatches to a properties
                    # model. Restore that nesting in actionable write feedback.
                    kind = raw.get("node_type") if isinstance(raw, dict) else None
                    fields = set(AUTHORING_MODELS[kind].model_fields) if kind in AUTHORING_MODELS else set()
                    supplied = raw.get("properties", {}) if isinstance(raw, dict) else {}
                    for error, detail in zip(errors, exc.errors(), strict=True):
                        first = str(detail["loc"][0]) if detail["loc"] else ""
                        if first != "properties" and (first in fields or isinstance(supplied, dict) and first in supplied):
                            error.path = error.path.replace(f"upserts.{index}.", f"upserts.{index}.properties.", 1)
                    return GraphResult(
                        status="rejected",
                        revision=session.graph.working_revision,
                        errors=errors,
                    ).model_dump(mode="json")
            value_ids = []
            for index, proposal in enumerate(values or []):
                try:
                    generated = value_asset(
                        ValueWrite.model_validate(proposal),
                        session.graph.working.assets,
                    )
                except ValidationError as exc:
                    raise GraphRejected(
                        input_issues(exc, path=f"values.{index}")
                    ) from exc
                value_ids.append(generated.id)
                parsed.append(generated)
            if session.mode == "direct":

                def normalize(text):
                    return " ".join(text.split()).casefold()

                question = normalize(session.record["question"])
                accepted = {a.id: a for a in session.graph.accepted.semantic_assets}
                checks = [
                    (f"upserts.{index}", asset) for index, asset in enumerate(parsed)
                ]
                from cortex.schema import GraphNode, NodeMergeConflict

                for index, operation in enumerate(conflict_operations or []):
                    if not isinstance(operation, dict):
                        raise GraphRejected(
                            [
                                issue(
                                    "INVALID_FIELD",
                                    "Conflict operation must be an object.",
                                    "Use set_conflict or resolve_conflict.",
                                    path=f"conflict_operations.{index}",
                                )
                            ]
                        )
                    if operation.get("operation") != "set_conflict":
                        continue
                    owner = GraphNode.model_validate(operation.get("node"))
                    conflict = NodeMergeConflict.model_validate(
                        operation.get("conflict")
                    )
                    for position, alternative in enumerate(conflict.alternatives):
                        content = GraphManager.resolve_alternative(owner, alternative)
                        checks.append(
                            (
                                f"conflict_operations.{index}.conflict.alternatives.{position}",
                                semantic_asset(
                                    owner.model_copy(
                                        update=content.model_dump(mode="json")
                                    )
                                ),
                            )
                        )
                for asset_path, asset in checks:
                    if asset.node_type != NodeType.GENERATION_RULE:
                        continue
                    prior = accepted.get(asset.id)
                    prior_phrases = {
                        normalize(phrase)
                        for phrase in (
                            prior.properties.triggers.question.phrases if prior else []
                        )
                    }
                    for position, phrase in enumerate(
                        asset.properties.triggers.question.phrases
                    ):
                        if (
                            normalize(phrase) not in prior_phrases
                            and normalize(phrase) not in question
                        ):
                            raise GraphRejected(
                                [
                                    issue(
                                        "QUESTION_TRIGGER_NOT_IN_SOURCE",
                                        "A new Direct Question trigger must occur in the Question.",
                                        "Copy a contiguous phrase from the Question or remove this trigger; only case and whitespace may differ.",
                                        path=f"{asset_path}.properties.triggers.question.phrases.{position}",
                                    )
                                ]
                            )
            for node_id in delete_ids or []:
                if node_id in {a.id for a in session.graph.accepted.assets}:
                    raise GraphRejected(
                        [
                            issue(
                                "OPERATION_NOT_ALLOWED",
                                "Learning cannot delete accepted or physical nodes.",
                                "Revise the existing semantic content or revert tentative changes.",
                                path="delete_ids",
                                details={"node_id": node_id},
                            )
                        ]
                    )
            result = _edit_graph_impl(
                edit_id,
                expected_revision,
                parsed,
                [*(revert_ids or []), *(delete_ids or [])],
                proposed_groups,
                edge_operations,
                conflict_operations,
                relation_contracts,
            )
            if value_ids:
                result["value_ids"] = value_ids
            return result
        except GraphRejected as exc:
            return GraphResult(
                status="rejected",
                revision=session.graph.working_revision,
                errors=exc.issues,
            ).model_dump(mode="json")
        except ValidationError as exc:
            return GraphResult(
                status="rejected",
                revision=session.graph.working_revision,
                errors=input_issues(exc),
            ).model_dump(mode="json")
        except ValueError as exc:
            return GraphResult(
                status="rejected",
                revision=session.graph.working_revision,
                errors=[
                    issue(
                        "INVALID_EDIT",
                        str(exc),
                        "Inspect the current graph and matching type guide, then correct the request.",
                    )
                ],
            ).model_dump(mode="json")

        except OSError:
            return GraphResult(
                status="error",
                revision=session.graph.working_revision,
                errors=[
                    issue(
                        "STORAGE_ERROR",
                        "Graph snapshot publication failed.",
                        "Check storage availability and retry the same edit_id; the working revision did not advance.",
                    )
                ],
            ).model_dump(mode="json")

    # LangChain's function-derived schema otherwise ignores unknown arguments.
    # Reject mixed legacy/new requests before any part of the edit can execute.
    edit_graph.args_schema.model_config["extra"] = "forbid"
    edit_graph.args_schema.model_rebuild(force=True)

    @tool
    def update_notes(notes: WorkingNotes) -> dict:
        """Replace short working notes: plan, evidence refs, hypotheses and failed paths.

        Notes are advisory, not graph facts. Keep them short for the next episode; never copy full SQL results, all round history or controller state here.
        """
        if len(notes.model_dump_json()) > session.config.agent_notes_characters:
            raise ValueError("working notes exceed configured character limit")
        session.notes = notes
        return {"status": "ok"}

    @tool
    def handoff(
        outcome: Literal["ready_for_validation", "unable_to_continue"],
        summary: str = "",
        direct_type_coverage: list[str] | None = None,
    ) -> dict:
        """End this episode and hand the current revision to the controller.

        Choose only case-supported types and relations; empty extraction is allowed. direct_type_coverage optionally records considered types, with no required type quota. Ready does not mean accepted or stable correct. Use unable_to_continue when the tools or evidence cannot support a repair.
        """
        if len(summary) > 2000:
            raise ValueError("handoff summary must be short")
        coverage = direct_type_coverage or []
        if len(coverage) != len(set(coverage)) or set(coverage) - AUTHORING_MODELS.keys():
            raise ValueError("direct_type_coverage must contain distinct supported semantic types")
        session.handoff_result = {
            "outcome": outcome,
            "summary": summary,
            "direct_type_coverage": coverage,
        }
        session.closed = True
        return {"status": "handed_off", "revision": session.graph.working_revision}

    tools = [
        inspect_graph,
        inspect_database,
        read_evidence,
        edit_graph,
        update_notes,
        handoff,
    ]
    # A misspelled read mode must produce feedback, not silently run the default.
    for item in tools:
        item.args_schema.model_config["extra"] = "forbid"
        item.args_schema.model_rebuild(force=True)
    return tools
