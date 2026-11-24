"""Selective current/source graph reads with real paths and one evidence section."""

from cortex.application.ontology_graph import GraphManager
from cortex.capabilities.graph_merge.graph.impact import content_consumers
from cortex.capabilities.graph_merge.graph.operations import PHYSICAL
from cortex.capabilities.graph_merge.graph.views import (
    edge_view,
    graph_view,
    node_view,
    physical_view,
)
from cortex.capabilities.graph_merge.identity.catalog import select_catalog_nodes
from cortex.schema.relation_contracts import effective_schema


class RelationReads:
    def __init__(self, context, evidence, scope=None, before=None):
        self.context, self.evidence, self.scope, self.before = (
            context,
            evidence,
            scope,
            before,
        )

    def lookup_catalog(
        self, table, columns=None, *, view="candidate", offset=0, limit=30
    ):
        if offset < 0 or not 1 <= limit <= 100:
            raise ValueError("Use offset >= 0 and limit between 1 and 100")
        nodes, edges, _ = graph_view(self.context, view, before=self.before)
        selected, information = select_catalog_nodes(nodes, table, columns)
        page = selected[offset : offset + limit]
        if self.scope:
            self.scope.observe([{"id": node.id} for node in page], view)
        return {
            "view": view,
            **information,
            "items": [physical_view(node, nodes, edges.values()) for node in page],
            "complete": offset + limit >= len(selected),
            "next_offset": offset + limit if offset + limit < len(selected) else None,
        }

    def read(
        self,
        mode,
        ids,
        fields=None,
        view="candidate",
        offset=0,
        limit=30,
        direction="both",
        edge_types=None,
    ):
        if offset < 0 or not 1 <= limit <= 100:
            raise ValueError("Use offset >= 0 and limit between 1 and 100")
        if mode not in {"items", "relations", "consumers", "contracts"}:
            raise ValueError("mode must be items, relations, consumers, or contracts")
        ctx = self.context
        nodes, edges, graph = graph_view(ctx, view, before=self.before)
        ids = list(dict.fromkeys(ids or []))
        if not ids and mode != "contracts":
            raise ValueError(
                "Supply exact node/edge IDs; use lookup_catalog to locate a table or column"
            )
        rows, errors, refs = [], [], []
        if mode == "contracts":
            rows = [
                r.model_dump(mode="json")
                for r in effective_schema(ctx.schema, graph).edge_types
                if not ids or r.id in ids
            ]
        elif mode == "consumers":
            rows = content_consumers(nodes.values(), ids)
        else:
            objects = [nodes.get(k) or edges.get(k) for k in ids]
            if mode == "relations":
                objects = [
                    e
                    for e in edges.values()
                    if (
                        e.id in ids
                        or (direction != "incoming" and e.from_node_id in ids)
                        or (direction != "outgoing" and e.to_node_id in ids)
                    )
                    and (not edge_types or e.edge_type in edge_types)
                ]
            total = len(objects)
            for obj in objects[offset : offset + limit]:
                if obj is None:
                    continue
                refs.extend(obj.sources)
                row = (
                    self._fields(obj, fields, errors)
                    if fields
                    else (
                        node_view(obj, evidence_ids=self.evidence.by_source)
                        if obj.id in nodes
                        else edge_view(obj, self.evidence.by_source)
                    )
                )
                if obj.id in nodes and obj.node_type in PHYSICAL and not fields:
                    row = physical_view(obj, nodes, edges.values())
                if getattr(obj, "alternatives", ()):
                    refs.extend(s for a in obj.alternatives for s in a.sources)
                    if not fields:
                        row["alternatives"] = [
                            {
                                "alternative_id": a.alternative_id,
                                "content": GraphManager.resolve_alternative(
                                    obj, a
                                ).model_dump(mode="json"),
                                "evidence_ids": self.evidence.ids_for(a.sources),
                            }
                            for a in obj.alternatives
                        ]
                rows.append(row)
            if self.scope:
                self.scope.observe(rows, view)
            result = {
                "view": view,
                "revision": getattr(ctx.active_batch, "revision", 0)
                if getattr(ctx, "active_batch", None)
                else 0,
                "items": rows,
                "complete": offset + limit >= total,
                "next_offset": offset + limit if offset + limit < total else None,
            }
            if mode == "items":
                result["missing_ids"] = sorted(set(ids) - nodes.keys() - edges.keys())
                errors.extend(
                    {"id": key, "error": "Unknown ID in the requested view"}
                    for key in result["missing_ids"]
                )
            if mode == "relations":
                endpoints = {
                    k
                    for obj in objects[offset : offset + limit]
                    for k in (obj.from_node_id, obj.to_node_id)
                }
                result["endpoint_nodes"] = [
                    node_view(nodes[k])
                    if nodes[k].node_type not in PHYSICAL
                    else physical_view(nodes[k], nodes, edges.values())
                    for k in sorted(endpoints)
                ]
                if self.scope:
                    self.scope.observe(result["endpoint_nodes"], view)
            if errors:
                result["read_errors"] = errors
            return self.evidence.append(result, self.evidence.ids_for(refs))
        return {
            "view": view,
            "items": rows[offset : offset + limit],
            "complete": offset + limit >= len(rows),
            "next_offset": offset + limit if offset + limit < len(rows) else None,
        }

    def _fields(self, obj, fields, errors):
        raw = obj.model_dump(mode="json")
        is_node = "node_type" in raw
        row = {"id": obj.id}
        for field in fields:
            if field in {"evidence_ids", "common_evidence_ids"}:
                refs = (
                    getattr(obj, "common_sources", ())
                    if field == "common_evidence_ids"
                    else (
                        *obj.sources,
                        *(
                            s
                            for a in getattr(obj, "alternatives", ())
                            for s in a.sources
                        ),
                    )
                )
                row[field] = self.evidence.ids_for(refs)
                continue
            if field == "alternatives" and is_node:
                row[field] = [
                    {
                        "alternative_id": a.alternative_id,
                        "content": GraphManager.resolve_alternative(obj, a).model_dump(
                            mode="json"
                        ),
                        "evidence_ids": self.evidence.ids_for(a.sources),
                    }
                    for a in obj.alternatives
                ]
                continue
            if field == "content" and is_node and obj.node_type not in PHYSICAL:
                row[field] = {
                    k: raw[k]
                    for k in ("name", "description", "aliases", "tags", "properties")
                }
                continue
            if field in {
                "sources",
                "common_sources",
                "*",
                "properties",
                "source_metadata",
                "table_schema",
            }:
                errors.append(
                    {
                        "id": obj.id,
                        "field": field,
                        "error": "Use exact paths, evidence_ids for attribution, or content for node reconstruction",
                    }
                )
                continue
            value, exists = raw, True
            parts = field.split(".")
            for part in parts:
                if not isinstance(value, dict) or part not in value:
                    exists = False
                    break
                value = value[part]
            if not exists:
                declared = (
                    self.context.schema.effective_node_fields(obj.node_type)
                    if is_node
                    else {"alternative_ids": None}
                )
                if (
                    len(parts) == 2
                    and parts[0] == "properties"
                    and parts[1] in declared
                ):
                    value = None
                    row.setdefault("absent_fields", []).append(field)
                else:
                    errors.append(
                        {
                            "id": obj.id,
                            "field": field,
                            "error": "Absent or inapplicable field",
                            "available_fields": sorted(
                                (
                                    raw.keys()
                                    - {"sources", "reference_origin", "properties"}
                                )
                                | {"properties." + k for k in obj.properties}
                            ),
                        }
                    )
                    continue
            dest = row
            for part in parts[:-1]:
                dest = dest.setdefault(part, {})
            dest[parts[-1]] = value
        return row
