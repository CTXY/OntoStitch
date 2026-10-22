"""Six domain tools. No tool can alter controller facts or acceptance decisions."""

from __future__ import annotations

import json
from typing import Any, Literal

from langchain_core.tools import tool

from cortex.capabilities.sql_training.graph.mutations import GraphEdit, apply_edit
from cortex.capabilities.sql_training.graph.snapshots import (
    semantic_diff,
)
from cortex.capabilities.sql_training.learning.database import (
    GroundingDatabase,
    investigate_database,
)
from cortex.capabilities.sql_training.learning.session import (
    LearningSession,
    WorkingNotes,
)
from cortex.capabilities.sql_training.llm.generation_rule_taxonomy import (
    render_generation_rule_taxonomy,
)
from cortex.capabilities.sql_training.models import (
    ConceptProperties,
    GenerationRuleProperties,
    MetricProperties,
)
from cortex.schema.ontology_asset_definitions import render_ontology_asset_contract

READ_TOOLS = frozenset({"inspect_graph", "inspect_database", "read_evidence"})
WRITE_TOOLS = frozenset({"edit_graph", "update_notes", "handoff"})


def build_tools(session: LearningSession):
    @tool
    def inspect_graph(
        mode: Literal["nodes", "node", "dependencies", "diff", "guide"] = "nodes",
        node_id: str = "",
        revision: str = "",
        node_type: str = "",
        offset: int = 0,
        limit: int = 20,
        guide_section: Literal["schema", "contract", "taxonomy"] = "schema",
    ) -> dict:
        """Read graph nodes, full definitions, dependents, tentative diff or type guide.

        Defaults to current Working; copy canonical IDs from the results. Physical
        Table/Column/Value nodes are already in the graph. Use guide before authoring
        an unfamiliar type; node_type is concept, metric or generation_rule. Guide
        defaults to compact exact schema; full type rules are linked by evidence ID.
        offset/limit count node rows, not guide characters; read_evidence pages text.
        """
        if offset < 0 or not 1 <= limit <= 50:
            raise ValueError("offset >= 0 and 1 <= limit <= 50 required")
        snap = session.graph.read(revision or session.graph.working_revision)
        if mode == "guide":
            if node_type and node_type not in {"concept", "metric", "generation_rule"}:
                raise ValueError("unknown semantic type")
            sections = (node_type,) if node_type else ("common", "owners")
            text = render_ontology_asset_contract(sections)
            if guide_section == "contract":
                return session.summarize({"contract": text, "revision": snap.revision})
            if guide_section == "taxonomy":
                return session.summarize(
                    {"taxonomy": render_generation_rule_taxonomy()}
                )
            contract_ref = session.evidence.put("type_contract", {"contract": text})
            properties_models = {
                "concept": ConceptProperties,
                "metric": MetricProperties,
                "generation_rule": GenerationRuleProperties,
            }
            schema = (
                properties_models[node_type].model_json_schema() if node_type else None
            )
            return session.summarize(
                {
                    "contract_ref": contract_ref,
                    "instruction": "Exact fields below; read contract_ref for detailed domain rules when needed. Taxonomy is available via guide_section=taxonomy.",
                    "properties_schema": schema,
                    "asset_fields": [
                        "id",
                        "node_type",
                        "name",
                        "aliases",
                        "description",
                        "tags",
                        "properties",
                    ],
                    "revision": snap.revision,
                }
            )
        by_id = {a.id: a for a in snap.assets}
        if mode == "node":
            if node_id not in by_id:
                raise ValueError("node does not exist at this revision")
            result: Any = by_id[node_id].model_dump(mode="json")
        elif mode == "dependencies":
            if node_id not in by_id:
                raise ValueError("node does not exist at this revision")
            result = {
                "references": by_id[node_id].references(),
                "referenced_by": sorted(
                    a.id for a in snap.assets if node_id in a.references()
                ),
            }
        elif mode == "diff":
            before = session.graph.accepted
            changed = semantic_diff(before, snap)
            result = [
                {
                    "node_id": key,
                    "before": next(
                        (
                            a.model_dump(mode="json")
                            for a in before.semantic_assets
                            if a.id == key
                        ),
                        None,
                    ),
                    "after": by_id[key].model_dump(mode="json")
                    if key in by_id
                    else None,
                }
                for key in changed
            ]
        else:
            rows = [
                a
                for a in snap.assets
                if not node_type or a.node_type.value == node_type
            ]
            result = {
                "nodes": [
                    {"id": a.id, "type": a.node_type.value, "name": a.name}
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
        limit: int = 10,
    ) -> dict:
        """Investigate the configured database read-only within its frozen catalog.

        Inspect metadata, bounded sample, exact categorical value, statistics, or
        one SELECT/CTE query. Results include a local evidence reference. This tool
        does not create graph nodes; Concept edits materialize verified values.
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

        Use evidence IDs supplied in context or tools; offset/limit page serialized
        text without injecting every historical round into working memory.
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

    @tool
    def edit_graph(edit: dict[str, Any]) -> dict:
        """Atomically create/update multiple semantic nodes, or revert tentative nodes.

        Supply the current expected_revision, unique edit_id and FULL NodeAsset per
        create/update. Never change ID/type or physical nodes. All mutually needed
        nodes belong in one call. proposed_groups links whole nodes to fixed targets;
        acceptance is decided independently after handoff and validation.
        """
        edit = GraphEdit.model_validate(edit)
        targets = {str(t["target_id"]) for t in session.targets}
        if session.mode == "repair" and any(
            set(g.target_ids) - targets for g in edit.proposed_groups
        ):
            raise ValueError(
                "proposed_groups must reference this round's fixed target IDs"
            )
        before = session.graph.working
        result = apply_edit(
            session.graph,
            edit,
            database=GroundingDatabase(session),
            source_id=str(session.record["source_id"]),
        )
        session.groups.extend(edit.proposed_groups)
        return {
            "status": "ok",
            "applied_revision": result.revision,
            "working_revision": session.graph.working_revision,
            "changed_ids": semantic_diff(before, session.graph.working),
        }

    @tool
    def update_notes(notes: WorkingNotes) -> dict:
        """Replace short working notes: plan, evidence refs, hypotheses and failed paths.

        Notes are advisory, not graph facts. Keep them short for the next episode;
        never copy full SQL results, all round history or controller state here.
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

        Direct must explicitly cover concept, metric and generation_rule (empty
        extraction is allowed). Ready does not mean accepted or stable correct.
        Use unable_to_continue when the tools or evidence cannot support a repair.
        """
        if len(summary) > 2000:
            raise ValueError("handoff summary must be short")
        coverage = direct_type_coverage or []
        if (
            session.mode == "direct"
            and outcome == "ready_for_validation"
            and set(coverage) != {"concept", "metric", "generation_rule"}
        ):
            raise ValueError(
                "Direct requires coverage of concept, metric and generation_rule"
            )
        session.handoff_result = {
            "outcome": outcome,
            "summary": summary,
            "direct_type_coverage": coverage,
        }
        session.closed = True
        return {"status": "handed_off", "revision": session.graph.working_revision}

    return [
        inspect_graph,
        inspect_database,
        read_evidence,
        edit_graph,
        update_notes,
        handoff,
    ]
