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
            return {'guidance': (
                'Use MergePatch.conflicts or integrate_groups with complete alternatives '
                '(alternative_id, content, evidence_ids) and conflict_points. '
                'common_evidence_ids attributes input knowledge preserved in every complete branch; '
                'it does not prove each stronger branch assertion. Existing branch support remains separate. '
                'Identity integrations require explicit source-node/branch dispositions, including ordinary '
                'members and old common scopes when the result is a conflict. '
                'Code compacts storage and resolves exact references. Do not write set_conflict, '
                'raw sources, GraphNode storage envelopes, or internal overrides. '
                'Scope branch-specific edges with properties.alternative_ids on their source owner.')}
        raw = {item.name: item for item in build_tools(context)}
        return raw["read_guidance"].invoke({"topic": topic})

    @tool
    def inspect_draft() -> dict:
        """Read this task's latest unapplied draft and its validation issues."""
        draft = draft_provider() if draft_provider is not None else getattr(context, "active_draft", None)
        return {"status": "no_draft"} if draft is None else {
            "status": "draft", "draft": draft,
            **({'issues': list(context.task_draft_errors)} if getattr(context, 'task_draft_errors', ()) else {})}

    return {item.name: item for item in (inspect_graph, lookup_catalog, read_evidence, read_guidance, inspect_draft)}
