"""Shared read tools for Writers and Assessments over the same task snapshots."""
from typing import Literal

from langchain_core.tools import tool

from cortex.capabilities.graph_merge.integration.evidence_refs import EvidencePresentation
from cortex.capabilities.graph_merge.integration.tools import build_tools
from cortex.capabilities.graph_merge.llm.relation_reads import RelationReads


def build_read_tools(context, payload, *, scope=None, before=None, draft_provider=None):
    """Return named LangChain tools; draft_provider returns current task draft JSON."""
    evidence = scope.evidence if scope is not None else EvidencePresentation(context, payload.get("evidence", []))
    reader = RelationReads(context, evidence, scope, before=before)

    @tool
    def inspect_graph(
        mode: Literal["items", "relations", "consumers", "contracts"],
        ids: list[str] | None = None,
        fields: list[str] | None = None,
        view: Literal["input", "before", "candidate"] = "candidate",
        offset: int = 0,
        limit: int = 30,
        direction: Literal["incoming", "outgoing", "both"] = "both",
        edge_types: list[str] | None = None,
    ) -> dict:
        """Read exact objects or adjacent relations in one explicit view.

        items accepts node/edge IDs; fields uses real paths such as
        properties.targets, properties.expression or properties.relation_metadata.
        content reads complete node business content; alternatives reads full
        resolved branches. Bare targets and whole properties are not selectors.
        relations filters direction and predicates before
        pagination. An incomplete page does not prove absence. Repeated reads
        return the selected current values. Reads do not authorize graph edits.
        """
        return reader.read(mode, ids, fields, view, offset, limit, direction, edge_types)

    @tool
    def lookup_catalog(
        table: str,
        columns: list[str] | None = None,
        view: Literal["input", "before", "candidate"] = "candidate",
        offset: int = 0,
        limit: int = 30,
    ) -> dict:
        """Locate physical IDs by exact table name/ID and optional column names.

        Returns table/column names and existing Concept grounding owners. This
        is a read-only catalog lookup, not SQL execution or a semantic search.
        """
        return reader.lookup_catalog(table, columns, view=view, offset=offset, limit=limit)

    @tool
    def read_evidence(ids: list[str]) -> dict:
        """Read original Question, Gold SQL and evidence by the displayed IDs."""
        return evidence.read(ids, refresh=True)

    @tool
    def read_guidance(topic: Literal["concept", "metric", "generation_rule", "event", "operations", "conflicts", "relations"]) -> dict:
        """Read type/operation guidance needed for the current decision."""
        if topic == 'conflicts':
            from importlib.resources import files
            return {'guidance': files('cortex.capabilities.graph_merge.llm.prompts').joinpath('conflict_contract.md').read_text()}
        raw = {item.name: item for item in build_tools(context)}
        return raw["read_guidance"].invoke({"topic": topic})

    @tool
    def read_source_units(owner_ids: list[str]) -> dict:
        """Read exact controller-owned source units from fixed before.

        Planned identity owners/members return their complete identity group;
        other owners return standalone scopes. Structural shared fields do not
        create source units. Reading never authorizes unrelated writes.
        """
        from cortex.capabilities.graph_merge.integration.source_units import source_unit_manifest
        base = before or getattr(context, 'task_before', None)
        if base is None:
            raise ValueError('Fixed before graph is unavailable')
        if not owner_ids:
            raise ValueError('Provide at least one existing owner ID')
        nodes = {n.id: n for n in base.graph.nodes}
        unknown = set(owner_ids) - nodes.keys()
        if unknown:
            raise ValueError(f'Unknown before owners: {sorted(unknown)}')
        groups = [g for g in getattr(context, 'task_identity_groups', ())
                  if set(owner_ids).intersection(g['member_ids'])]
        return {'view': 'before', 'source_unit_manifest': source_unit_manifest(base, groups, owner_ids)}

    @tool
    def inspect_draft(owner_ids: list[str] | None = None, sections: list[str] | None = None) -> dict:
        """Read latest unapplied draft/errors, optionally only selected owners/sections.

        sections names top-level operations, or source_routes/content/alternatives/
        conflict_points within integrate_groups and conflicts. A filtered result
        is a projection, not a replacement draft. No accepted graph is changed.
        """
        draft = draft_provider() if draft_provider is not None else getattr(context, "active_draft", None)
        if draft is not None and (owner_ids is not None or sections is not None):
            nested = {'source_routes', 'content', 'alternatives', 'conflict_points'}
            unknown = set(sections or ()) - set(draft) - nested
            if unknown:
                raise ValueError(f'Unknown draft sections: {sorted(unknown)}')
            selected = {}
            for key, value in draft.items():
                if sections is not None and key not in sections and not (
                    key in {'integrate_groups', 'conflicts'} and nested.intersection(sections)):
                    continue
                if isinstance(value, list) and owner_ids is not None:
                    value = [row for row in value if isinstance(row, dict) and
                             (row.get('owner_id') or row.get('node_id') or row.get('id')) in owner_ids]
                if key in {'integrate_groups', 'conflicts'} and sections is not None and key not in sections:
                    value = [{k: v for k, v in row.items() if k in {'owner_id', 'node_id'} or k in sections}
                             for row in value]
                selected[key] = value
            draft = selected
        return {"status": "no_draft"} if draft is None else {
            "status": "draft", "draft": draft,
            **({'projection': {'owner_ids': owner_ids, 'sections': sections}}
               if owner_ids is not None or sections is not None else {}),
            **({'issues': list(context.task_draft_errors)} if getattr(context, 'task_draft_errors', ()) else {})}

    return {item.name: item for item in (inspect_graph, lookup_catalog, read_evidence, read_guidance,
                                        read_source_units, inspect_draft)}
