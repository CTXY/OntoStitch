"""Append-only graph-local relation definitions; no inferred business edges."""

from __future__ import annotations

from collections.abc import Iterable

from cortex.schema.models import EdgeType, SchemaDefinition
from cortex.schema.relation_vocabulary import legacy_relation_contracts


def merge_relation_contracts(*groups: Iterable[EdgeType]) -> tuple[EdgeType, ...]:
    contracts: dict[str, EdgeType] = {}
    for group in groups:
        for raw in group:
            contract = EdgeType.model_validate(raw)
            for field in ("name", "description", "direction_semantics", "evidence_requirement"):
                value = getattr(contract, field)
                if not isinstance(value, str) or not value.strip():
                    raise ValueError(f"relation contract {contract.id!r} requires {field}")
            if contract.id in contracts and contracts[contract.id] != contract:
                raise ValueError(f"relation contract {contract.id!r} cannot be redefined")
            contracts[contract.id] = contract
    return tuple(contracts[key] for key in sorted(contracts))


def authoring_schema(base: SchemaDefinition, graph) -> SchemaDefinition:
    """Only core/base and explicitly saved local contracts authorize new writes."""
    local = merge_relation_contracts(graph.relation_contracts)
    existing = base.edge_type_index()
    for contract in local:
        if contract.id in existing and existing[contract.id] != contract:
            raise ValueError(f"relation contract {contract.id!r} cannot override an existing definition")
        existing[contract.id] = contract
    result = SchemaDefinition(node_types=base.node_types, edge_types=tuple(existing.values()))
    for contract in existing.values():
        if not contract.extends:
            continue
        parent = base.edge_type_index().get(contract.extends)
        if parent is None or parent.extends or contract.id == parent.id:
            raise ValueError("attribute specialization requires the root has_attribute parent")
        for child_types, parent_types in ((contract.from_node_types, parent.from_node_types),
                                         (contract.to_node_types, parent.to_node_types)):
            if any(not any(result.is_node_type_compatible(t, p) for p in parent_types) for t in child_types):
                raise ValueError("attribute specialization cannot widen parent endpoints")
        if contract.directionality != parent.directionality:
            raise ValueError("attribute specialization cannot change parent directionality")
        if any(key in contract.fields and contract.fields[key] != field for key, field in parent.fields.items()):
            raise ValueError("attribute specialization cannot redefine inherited fields")
    return result


def effective_edge_fields(schema, contract):
    """Do not rewrite raw contracts: effective_schema is called repeatedly."""
    parent = schema.edge_type_index().get(contract.extends) if contract.extends else None
    return {**(parent.fields if parent else {}), **contract.fields}


def effective_schema(base: SchemaDefinition, graph) -> SchemaDefinition:
    """Validate old saved edges too; compatibility contracts never authorize writes."""
    current = authoring_schema(base, graph)
    existing = current.edge_type_index()
    used = {edge.edge_type for edge in graph.edges}
    historical = [r for r in legacy_relation_contracts() if r.id in used and r.id not in existing]
    return SchemaDefinition(node_types=current.node_types, edge_types=(*current.edge_types, *historical))


def unregistered_relation_types(base: SchemaDefinition, graph) -> list[str]:
    known = authoring_schema(base, graph).edge_type_index()
    return sorted({edge.edge_type for edge in graph.edges if edge.edge_type not in known})
