"""Recall inspectable patterns, never decide identity/conflict/redundancy."""

from collections import defaultdict

import sqlglot
from sqlglot import exp

from cortex.capabilities.graph_merge.integration.context import PHYSICAL
from cortex.schema.relation_vocabulary import ABSTRACT_RELATIONS


def projected_columns(sql, nodes, dialect=None):
    """Only unambiguous direct SELECT expressions; not general SQL lineage."""
    try:
        tree = sqlglot.parse_one(sql, dialect=dialect)
    except (ValueError, sqlglot.errors.SqlglotError):
        return set()
    if not isinstance(tree, exp.Select):
        return set()
    from cortex.capabilities.sql_training.extractors.sql_normalizer import (
        physical_column_sources,
    )

    by_name = {}
    table_columns = defaultdict(set)
    for n in nodes.values():
        if n.node_type == "column":
            owner = nodes.get(n.properties.get("table_ref"))
            if owner:
                table = owner.properties.get("table_name")
                column = n.properties.get("column_name")
                by_name[table, column] = n.id
                table_columns[table].add(column)
    lineage = physical_column_sources(tree, dict(table_columns))
    return {
        by_name[pair]
        for projection in tree.expressions
        for col in projection.find_all(exp.Column)
        if (pair := lineage.get(id(col))) in by_name
    }


def inspection_signals(context, preview, *, origins=None):
    nodes = {n.id: n for n in preview.state.graph.nodes}
    edges = [
        e
        for e in preview.state.graph.edges
        if nodes[e.from_node_id].node_type not in PHYSICAL
        and nodes[e.to_node_id].node_type not in PHYSICAL
    ]
    signals = []

    def add(kind, keys, **details):
        signals.append(
            {"id": kind + ":" + ",".join(sorted(keys)), "kind": kind, **details}
        )

    owners = defaultdict(set)
    for edge in preview.state.graph.edges:
        if edge.edge_type in ABSTRACT_RELATIONS:
            add(
                "abstract_predicate",
                [edge.id],
                edge_ids=[edge.id],
                node_ids=[edge.from_node_id, edge.to_node_id],
            )
        if (
            edge.edge_type == "grounded_on"
            and nodes[edge.from_node_id].node_type == "concept"
        ):
            owners[edge.to_node_id].add(edge.from_node_id)
    for endpoint, claimants in sorted(owners.items()):
        if len(claimants) > 1:
            add(
                "binding_contest",
                [endpoint],
                node_ids=sorted(claimants),
                physical_id=endpoint,
                edge_ids=sorted(e.id for e in preview.state.graph.edges
                    if e.edge_type == "grounded_on" and e.to_node_id == endpoint
                    and e.from_node_id in claimants),
            )
    pairs = defaultdict(list)
    neighbors = defaultdict(list)
    adj = defaultdict(list)
    for e in edges:
        pairs[e.from_node_id, e.to_node_id].append(e)
        neighbors[e.from_node_id, e.edge_type].append(e)
        adj[e.from_node_id].append(e)
    for (a, b), group in sorted(pairs.items()):
        if len(group) > 1:
            add("parallel_assertions", [a, b], edge_ids=[e.id for e in group])
    triangles = defaultdict(set)
    for e in edges:
        for second in adj[e.to_node_id]:
            if e.from_node_id == second.to_node_id:
                continue
            direct = pairs.get((e.from_node_id, second.to_node_id), [])
            if direct:
                triangles[e.from_node_id, e.to_node_id, second.to_node_id].update(
                    [e.id, second.id, *[d.id for d in direct]]
                )
    for chain, ids in sorted(triangles.items()):
        add("triangle", list(chain), node_ids=list(chain), edge_ids=sorted(ids))
    for (owner, predicate), group in sorted(neighbors.items()):
        if (
            nodes[owner].node_type in {"concept", "event"}
            and len({e.to_node_id for e in group}) > 1
        ):
            add(
                "same_role_neighbors",
                [owner, predicate],
                edge_ids=[e.id for e in group],
            )
    rules = [n for n in nodes.values() if n.node_type == "generation_rule"]
    targets = defaultdict(set)
    for e in edges:
        if e.edge_type == "applies_to":
            targets[e.from_node_id].add(e.to_node_id)
    by_operand = defaultdict(set)
    for rule in rules:
        for operand in targets[rule.id]:
            by_operand[operand].add(rule.id)
    for operand, members in sorted(by_operand.items()):
        if len(members) > 1:
            add(
                "related_decisions",
                [operand, *sorted(members)],
                node_ids=sorted(members),
                shared_operands=[operand],
            )
    # Resolve the unchanged lineage once per scan, not inside the
    # evidence × source-node × current-node loop. This is only an index over
    # recorded provenance; it neither infers identity nor changes signals.
    origin_concepts = defaultdict(set)
    if origins is not None:
        for key, node in nodes.items():
            if node.node_type == "concept":
                for ref in origins.origin_refs("node", key):
                    if ref.kind == "node":
                        origin_concepts[ref.id].add(key)
    choices = defaultdict(dict)
    for evidence in context.evidence:
        selected = projected_columns(
            evidence.content.get("sql", ""),
            context.nodes,
            getattr(context, "sql_dialect", None),
        )
        if not selected:
            continue
        for source in context.raw_nodes.values():
            if source.node_type != "concept" or not any(
                s.source_id == evidence.source.source_id for s in source.sources
            ):
                continue
            candidates = {preview.node_map.get(source.id, source.id)}
            if origins is not None:
                candidates.update(origin_concepts[source.id])
            refs = {
                context.physical_map.get(ref, ref)
                for group in source.properties.get("targets", [])
                for ref in group
            }
            chosen = selected & refs
            for canonical in sorted(candidates & nodes.keys()):
                if not chosen or nodes[canonical].node_type != "concept":
                    continue
                choices[canonical][evidence.source.source_id] = {
                    "source_id": evidence.source.source_id,
                    "source_node_id": source.id,
                    "selected_physical_inputs": sorted(chosen),
                    "evidence_id": evidence.id,
                }
    for canonical, variants in sorted(choices.items()):
        if len({tuple(v["selected_physical_inputs"]) for v in variants.values()}) > 1:
            add(
                "source_selection_variants",
                [canonical],
                node_ids=[canonical],
                variants=list(variants.values()),
            )
    values = defaultdict(list)
    for e in preview.state.graph.edges:
        target = nodes[e.to_node_id]
        if (
            e.edge_type == "grounded_on"
            and nodes[e.from_node_id].node_type == "concept"
            and target.node_type == "dimension_value"
        ):
            values[target.properties["column_ref"]].append(
                {"concept": e.from_node_id, "value": target.id}
            )
    for column, items in sorted(values.items()):
        if (
            len({x["value"] for x in items}) > 1
            and len({x["concept"] for x in items}) > 1
        ):
            add("value_concepts", [column], column_id=column, members=items)
    return signals
