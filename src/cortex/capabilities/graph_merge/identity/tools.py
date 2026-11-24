"""Read-only, paginated access to source and candidate graphs."""

import json

from langchain_core.tools import tool

from cortex.capabilities.graph_merge.graph.impact import content_consumers
from cortex.capabilities.graph_merge.graph.operations import (
    PHYSICAL,
    semantic_relations_first,
)
from cortex.capabilities.graph_merge.identity.catalog import select_catalog_nodes
from cortex.capabilities.sql_training.llm.prompts.asset_contract import (
    render_ontology_asset_contract,
)
from cortex.schema.relation_contracts import effective_schema


def build_tools(context, *, visible_source_ids=()):
    # Read authorization spans phases; conversational visibility does not.
    visible_sources = set(visible_source_ids)

    @tool
    def inspect_graph(
        mode: str,
        ids: list[str] | None = None,
        view: str = "input",
        offset: int = 0,
        limit: int = 40,
        table: str | None = None,
        column_names: list[str] | None = None,
        edge_types: list[str] | None = None,
    ) -> dict:
        """Read nodes, relations, consumers, candidates, or contracts. Paginate using next_offset.

        input uses immutable source-qualified handles, candidate uses the current preview.
        nodes: complete definitions. For catalogue lookup use table (exact name or ID),
        optionally column_names (exact names). Do not combine these selectors with ids.
        relations: adjacent edges; edge_types filters exact predicate IDs BEFORE pagination.
        To inspect joins request edge_types=["joins_with"], not every table ownership edge;
        consumers: declared reverse content references and relation groups, not adjacency; candidates: one or more seed IDs
        from the selected view, including new nodes, matched against source nodes. Multiple
        seeds return pages keyed by seed ID; paginate each independently. contracts:
        predicate IDs or no IDs for all. IDs are never interpreted as SQL or instructions.
        relations without IDs returns adjacency of read local semantic nodes (and new candidate
        nodes), including grounding. Supply node/edge IDs to inspect the physical catalogue.
        """
        if view not in {"input", "candidate"} or offset < 0:
            return {"error": "Use input/candidate and a nonnegative offset"}
        if (table is not None or column_names is not None) and (
            mode != "nodes" or ids or table is None
        ):
            return {
                "error": "Catalogue selectors require mode=nodes, table, and no ids"
            }
        if edge_types is not None and (mode != "relations" or not edge_types):
            return {"error": "edge_types requires mode=relations and a nonempty list"}
        limit = max(1, min(100, limit))
        ids = list(dict.fromkeys(ids or []))
        if view == "candidate" and context.candidate is None:
            return {"error": "No candidate yet; inspect input"}
        nodes = (
            context.nodes
            if view == "input"
            else {n.id: n for n in context.candidate.graph.nodes}
        )
        edges = (
            context.edges
            if view == "input"
            else {e.id: e for e in context.candidate.graph.edges}
        )
        selection_info = {}
        if mode == "candidates":
            if not ids or any(key not in nodes for key in ids):
                return {
                    "error": "Supply valid seed IDs from the selected view",
                    "missing_ids": sorted(set(ids) - nodes.keys()),
                }
            pages = {}
            for key in ids:
                page = context.candidates(
                    key, seed=nodes[key], offset=offset, limit=limit
                )
                page["items"] = [
                    {
                        **item,
                        **(
                            {
                                "name": context.nodes[item["id"]].name,
                                "already_in_context": True,
                            }
                            if item["id"] in visible_sources
                            else {
                                "node": context.nodes[item["id"]].model_dump(
                                    mode="json"
                                )
                            }
                        ),
                    }
                    for item in page["items"]
                ]
                context.read_ids.update(item["id"] for item in page["items"])
                visible_sources.update(item["id"] for item in page["items"])
                pages[key] = page
            result = pages[ids[0]] if len(ids) == 1 else {"pages": pages}
        else:
            if mode == "nodes":
                if table is not None:
                    selected, selection_info = select_catalog_nodes(
                        nodes, table, column_names
                    )
                else:
                    selected = (
                        [nodes[key] for key in ids if key in nodes]
                        if ids
                        else [n for n in nodes.values() if n.node_type not in PHYSICAL]
                    )
                items = [n.model_dump(mode="json") for n in selected]
            elif mode == "relations":
                selected_ids = (
                    set(ids)
                    if ids
                    else {
                        key
                        for key, node in nodes.items()
                        if node.node_type not in PHYSICAL
                        and (key in context.read_ids or key not in context.nodes)
                    }
                )
                items = [
                    e.model_dump(mode="json")
                    for e in semantic_relations_first(edges.values(), nodes)
                    if (
                        e.id in selected_ids
                        or e.from_node_id in selected_ids
                        or e.to_node_id in selected_ids
                    )
                    and (edge_types is None or e.edge_type in edge_types)
                ]
            elif mode == "consumers":
                items = content_consumers(nodes.values(), ids)
            elif mode == "contracts":
                graph = (
                    context.candidate.graph
                    if view == "candidate"
                    else context.union_state().graph
                )
                contracts = effective_schema(context.schema, graph).edge_types
                from cortex.capabilities.graph_merge.llm.prompt_language import (
                    english_contract,
                )

                items = [
                    english_contract(c.model_dump(mode="json"))
                    for c in contracts
                    if not ids or c.id in ids
                ]
            else:
                return {
                    "error": "mode must be nodes, relations, consumers, candidates or contracts"
                }
            page, size = [], 0
            for item in items[offset : offset + limit]:
                item_size = len(json.dumps(item, ensure_ascii=False))
                if page and size + item_size > 16000:
                    break
                page.append(item)
                size += item_size
            if mode == "nodes" and view == "input":
                context.read_ids.update(item["id"] for item in page)
                visible_sources.update(item["id"] for item in page)
            result = {
                **selection_info,
                "items": page,
                "total": len(items),
                "next_offset": offset + len(page)
                if offset + len(page) < len(items)
                else None,
            }
            if mode == "nodes":
                result["missing_ids"] = sorted(set(ids) - nodes.keys())
        return context.log(
            "inspect_graph",
            {
                "mode": mode,
                "ids": ids,
                "view": view,
                "offset": offset,
                "limit": limit,
                **({"table": table} if table is not None else {}),
                **({"column_names": column_names} if column_names is not None else {}),
                **({"edge_types": edge_types} if edge_types is not None else {}),
            },
            result,
        )

    @tool
    def read_evidence(ids: list[str], offset: int = 0, limit: int = 4) -> dict:
        """Read original NLQ, Gold SQL and supplied business evidence by evidence/source ID.

        No database probe is performed. Do not infer observed uniqueness/NULL rates from schema.
        """
        found = [
            row
            for row in context.evidence_rows()
            if row["id"] in ids
            or any(s["source_id"] in ids for s in row["source_refs"])
        ]
        limit, offset = max(1, min(10, limit)), max(0, offset)
        known = {row["id"] for row in found} | {
            s["source_id"] for row in found for s in row["source_refs"]
        }
        return context.log(
            "read_evidence",
            {"ids": ids, "offset": offset},
            {
                "items": found[offset : offset + limit],
                "missing_ids": sorted(set(ids) - known),
                "total": len(found),
                "next_offset": offset + limit if offset + limit < len(found) else None,
            },
        )

    @tool
    def read_guidance(topic: str) -> dict:
        """Read concept, metric, generation_rule, event, relations, conflicts, or operations guidance.

        Includes current node field schema. Read only relevant topics; operations returns
        the registry once, not a mandatory list of rules to extract or create.
        """
        if topic == "conflicts":
            from cortex.schema import GraphNode

            result = {
                "guidance": "Use set_conflict with a complete GraphNode: merge_status=conflict, consolidation_status=semantic_conflict, alternatives and conflict_points. Common properties contain only shared fields. Alternatives override the same nonempty set of whole declared fields using properties.FIELD paths; varying fields are omitted from the common properties. Each alternative resolves to a complete valid node. For varying name/description/aliases/tags use the common display placeholder (id/None/[]/[]). Node sources must be the alternatives' source union. Branch-specific outgoing edges use alternative_ids on the source owner. Preserve specific before/after alternative correspondences when changing branches. Do not invent node differences to store a relation-only conflict.",
                "node_schema": GraphNode.model_json_schema(),
            }
        elif topic == "operations":
            from cortex.capabilities.sql_training.llm.generation_rule_taxonomy import (
                operation_registry,
            )

            result = {"operation_registry": operation_registry()}
        elif topic == "relations":
            from cortex.capabilities.sql_training.llm.prompts.asset_contract import (
                RELATION_SCOPE_RULE,
            )

            result = {
                "guidance": RELATION_SCOPE_RULE,
            }
        elif topic in {"concept", "metric", "event", "generation_rule"}:
            result = {
                "guidance": render_ontology_asset_contract([topic]),
                "fields": {
                    key: value.model_dump(mode="json")
                    for key, value in context.schema.effective_node_fields(
                        topic
                    ).items()
                },
            }
        else:
            return {"error": "Unknown guidance topic"}
        return context.log("read_guidance", {"topic": topic}, result)

    @tool
    def lookup_catalog(
        table: str,
        columns: list[str] | None = None,
        view: str = "candidate",
        offset: int = 0,
        limit: int = 30,
    ) -> dict:
        """Locate physical IDs by exact table/column names, including current owners."""
        from cortex.capabilities.graph_merge.evidence import EvidencePresentation
        from cortex.capabilities.graph_merge.reconciliation.reads import RelationReads

        return RelationReads(context, EvidencePresentation(context)).lookup_catalog(
            table, columns, view=view, offset=offset, limit=limit
        )

    tools = [inspect_graph, lookup_catalog, read_evidence, read_guidance]
    return tools
