"""Immutable source handles, physical alignment and bounded candidate retrieval.

Retrieval scores are recall signals only. No score makes an identity decision.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from difflib import SequenceMatcher

from cortex.capabilities.graph_merge.identity.contract import exact_identity_key, node_content
from cortex.capabilities.graph_merge.graph.contracts import isolate_contracts
from cortex.capabilities.graph_merge.llm.packing import bounded_items
from cortex.schema import GraphSpace, GraphState

from cortex.capabilities.graph_merge.graph.operations import PHYSICAL, semantic_relations_first, remap_node, remap_edge, references, decision_references












def _tokens(value):
    return set(re.findall(r"\w+", value.casefold()))


class IntegrationContext:
    def __init__(self, existing, incoming, schema, *, evidence=(), top_k=5):
        self.existing, self.incoming, self.schema = existing, incoming, schema
        self.evidence, self.top_k = tuple(evidence), top_k
        occupied = {n.id for n in existing.graph.nodes} | {
            e.id for e in existing.graph.edges
        }
        prefix = "input::"
        handles = [f"n{i}" for i in range(len(incoming.graph.nodes))] + [
            f"e{i}" for i in range(len(incoming.graph.edges))
        ]
        while any(prefix + x in occupied for x in handles):
            prefix = "input::" + prefix
        self.input_node_map = {
            n.id: prefix + f"n{i}" for i, n in enumerate(incoming.graph.nodes)
        }
        self.input_edge_map = {
            e.id: prefix + f"e{i}" for i, e in enumerate(incoming.graph.edges)
        }
        # Source-local predicates may share a spelling without sharing a contract.
        self.contracts, predicate_map = isolate_contracts(
            existing.graph.relation_contracts,
            incoming.graph.relation_contracts,
            schema.edge_type_index(),
        )
        raw_nodes = {
            self.input_node_map[n.id]: remap_node(
                n, self.input_node_map, self.input_edge_map
            )
            for n in incoming.graph.nodes
        }
        self.raw_nodes = {**{n.id: n for n in existing.graph.nodes}, **raw_nodes}
        self.raw_edges = {e.id: e for e in existing.graph.edges}
        for e in incoming.graph.edges:
            edge = remap_edge(e, self.input_node_map, self.input_edge_map)
            self.raw_edges[edge.id] = edge.model_copy(
                update={"edge_type": predicate_map.get(edge.edge_type, edge.edge_type)}
            )
        # Validate the source fragment in its own namespace before physical or
        # semantic alignment. Conflicting local predicate names are already isolated.
        from cortex.application.ontology_graph import GraphManager

        fragment = GraphState(
            graph=GraphSpace(
                nodes=tuple(raw_nodes.values()),
                edges=tuple(
                    self.raw_edges[key] for key in self.input_edge_map.values()
                ),
                relation_contracts=self.contracts,
            )
        )
        prepared = GraphManager(schema).prepare_fragment(fragment, existing=existing)
        self.raw_edges.update({e.id: e for e in prepared.graph.edges})
        self.physical_map = {}
        nodes = {n.id: n for n in existing.graph.nodes}
        for kind in ("table", "column", "dimension_value"):
            policy = schema.node_type_index()[kind].identity_policy
            keys = {
                exact_identity_key(node_content(n), policy): n.id
                for n in nodes.values()
                if n.node_type == kind
            }
            for n in raw_nodes.values():
                if n.node_type != kind:
                    continue
                normalized = remap_node(n, self.physical_map)
                key = exact_identity_key(node_content(normalized), policy)
                canonical = keys.setdefault(key, n.id)
                self.physical_map[n.id] = canonical
                nodes.setdefault(canonical, normalized)
                nodes[canonical] = nodes[canonical].model_copy(
                    update={
                        "sources": tuple(
                            dict.fromkeys((*nodes[canonical].sources, *n.sources))
                        )
                    }
                )
        for n in raw_nodes.values():
            if n.node_type not in PHYSICAL:
                nodes[n.id] = remap_node(n, self.physical_map)
        self.nodes = nodes
        self.edges = {
            key: remap_edge(e, self.physical_map) for key, e in self.raw_edges.items()
        }
        self.seeds = {
            self.input_node_map[n.id]
            for n in incoming.graph.nodes
            if n.node_type not in PHYSICAL
        }
        for edge_id in self.input_edge_map.values():
            for endpoint in (
                self.edges[edge_id].from_node_id,
                self.edges[edge_id].to_node_id,
            ):
                if self.nodes[endpoint].node_type not in PHYSICAL:
                    self.seeds.add(endpoint)
        self.read_ids = set(self.seeds)
        self.tool_log = []
        self.candidate = None
        self.identity = None
        self.preview = None
        self._refresh_indexes()

    def _refresh_indexes(self):
        self.adjacency = defaultdict(list)
        self.consumers = defaultdict(set)
        for edge in self.edges.values():
            self.adjacency[edge.from_node_id].append(edge)
            if edge.to_node_id != edge.from_node_id:
                self.adjacency[edge.to_node_id].append(edge)
        for n in self.nodes.values():
            for ref in references(n):
                self.consumers[ref].add(n.id)
        owners = defaultdict(set)
        for e in self.edges.values():
            if (
                e.edge_type == "grounded_on"
                and self.nodes[e.from_node_id].node_type == "concept"
            ):
                owners[e.to_node_id].add(e.from_node_id)
        self.mandatory = sorted(
            {
                tuple(sorted((a, b)))
                for group in owners.values()
                for a in group
                for b in group
                if a < b and (a in self.seeds or b in self.seeds)
            }
        )

    def candidates(self, node_id, *, seed=None, offset=0, limit=None):
        seed = seed if seed is not None else self.nodes[node_id]
        names = " ".join([seed.name, *seed.aliases])
        terms = _tokens(names + " " + (seed.description or ""))
        seed_edges = (
            [
                e
                for e in self.candidate.graph.edges
                if node_id in (e.from_node_id, e.to_node_id)
            ]
            if self.candidate is not None and seed is not self.nodes.get(node_id)
            else self.adjacency.get(node_id, ())
        )
        roles = {(e.edge_type, e.from_node_id == node_id) for e in seed_edges}
        seed_nodes = (
            {n.id: n for n in self.candidate.graph.nodes}
            if self.candidate is not None and seed is not self.nodes.get(node_id)
            else self.nodes
        )
        refs = decision_references(seed, seed_nodes, seed_edges)
        mandatory = {
            b if a == node_id else a for a, b in self.mandatory if node_id in (a, b)
        }
        ranked = []
        for other in self.nodes.values():
            if other.id == node_id or other.node_type in PHYSICAL:
                continue
            text = " ".join([other.name, *other.aliases])
            words = _tokens(text + " " + (other.description or ""))
            anchors = refs & decision_references(
                other, self.nodes, self.adjacency[other.id]
            )
            other_roles = {
                (e.edge_type, e.from_node_id == other.id)
                for e in self.adjacency[other.id]
            }
            signals = []
            if anchors:
                signals.append("shared physical/semantic references")
            if roles & other_roles:
                signals.append("shared relation roles")
            shared_intents = (
                set(seed.properties.get("operation_intents", ()))
                & set(other.properties.get("operation_intents", ()))
                if seed.node_type == other.node_type == "generation_rule"
                else set()
            )
            if shared_intents:
                signals.append("shared operation intents (not identity)")
            score = (
                3 * len(anchors)
                + 2 * SequenceMatcher(None, names.casefold(), text.casefold()).ratio()
                + len(terms & words) / max(1, len(terms | words))
                + 0.2 * len(roles & other_roles)
                + 0.5 * bool(shared_intents)
            )
            ranked.append((other.id in mandatory, score, other.id, signals))
        ranked.sort(key=lambda x: (-x[0], -x[1], x[2]))
        limit = min(20, max(1, limit or self.top_k))
        page = ranked[offset : offset + limit]
        return {
            "items": [{"id": x[2], "mandatory": x[0], "signals": x[3]} for x in page],
            "total": len(ranked),
            "next_offset": offset + limit if offset + limit < len(ranked) else None,
        }

    def summary(self, node_id):
        n = self.nodes[node_id]
        return {
            "id": n.id,
            "type": n.node_type,
            "name": n.name,
            "description": n.description,
            "relation_count": len(self.adjacency[n.id]),
        }

    def payload(self, phase):
        self.phase = phase
        retrieved = {seed: self.candidates(seed) for seed in sorted(self.seeds)}
        selected = self.seeds | {
            x["id"] for page in retrieved.values() for x in page["items"]
        }
        selected |= {x for pair in self.mandatory for x in pair}
        selected |= {
            key
            for key in self.read_ids
            if key in self.nodes and self.nodes[key].node_type not in PHYSICAL
        }
        self.read_ids |= selected
        nodes = [
            self.nodes[key].model_dump(mode="json", exclude_defaults=True)
            for key in sorted(selected)
        ]
        edges = [
            e.model_dump(mode="json", exclude_defaults=True)
            for e in semantic_relations_first(self.edges.values(), self.nodes)
            if e.from_node_id in selected or e.to_node_id in selected
        ]
        # High-degree relation bodies are available via paginated tools, not silently truncated.
        initial_edges = edges[:60]
        endpoints = {
            e[key] for e in initial_edges for key in ("from_node_id", "to_node_id")
        }
        # Limit the directory, not evidence access: tools can still read any
        # source discovered while expanding the local graph.
        source_ids = {s.source_id for key in selected for s in self.nodes[key].sources}
        source_ids.update(
            s.source_id
            for key in selected
            for alternative in self.nodes[key].alternatives
            for s in alternative.sources
        )
        source_ids.update(
            s["source_id"] for edge in edges for s in edge.get("sources", ())
        )
        predicates = {edge["edge_type"] for edge in edges}
        evidence_rows = [
            row
            for row in self.evidence_rows()
            if any(s["source_id"] in source_ids for s in row["source_refs"])
        ]
        # Bindings/formulas and explicit adjacency identify relevant physical facts.
        # No graph expansion or business decision is inferred here.
        physical_ids = endpoints | {
            ref for key in selected for ref in references(self.nodes[key])
        }
        physical_facts = bounded_items(
            [
                self.nodes[key].model_dump(mode="json", exclude_defaults=True)
                for key in sorted(physical_ids)
                if key in self.nodes and self.nodes[key].node_type in PHYSICAL
            ],
            budget=8000,
        )
        return {
            "phase": phase,
            "incoming_seeds": sorted(self.seeds),
            "nodes": nodes,
            "candidate_ids": {
                key: [item["id"] for item in page["items"]]
                for key, page in retrieved.items()
            },
            "binding_contests": self.mandatory,
            "relations": initial_edges,
            "relations_total": len(edges),
            "more_relations": len(edges) > len(initial_edges),
            "endpoint_directory": [
                self.summary(key)
                for key in sorted(endpoints - selected)
                if key not in {row["id"] for row in physical_facts}
            ],
            "physical_facts": physical_facts,
            "evidence": bounded_items(evidence_rows),
            "evidence_directory": [
                {
                    "id": row["id"],
                    "source_ids": sorted({s["source_id"] for s in row["source_refs"]}),
                }
                for row in evidence_rows
            ],
            "local_contracts": [
                c.model_dump(mode="json") for c in self.contracts if c.id in predicates
            ],
        }

    def evidence_rows(self):
        """Share identical evidence bodies, retaining every original source reference."""
        grouped = {}
        for e in self.evidence:
            key = (e.id, json.dumps(e.content, sort_keys=True, ensure_ascii=False))
            row = grouped.setdefault(
                key,
                {
                    "id": e.id,
                    "case_id": e.case_id,
                    "content": e.content,
                    "source_refs": [],
                },
            )
            source = e.source.model_dump(mode="json")
            if source not in row["source_refs"]:
                row["source_refs"].append(source)
        return list(grouped.values())

    def union_state(self):
        """Unvalidated working input, never published as the accepted graph."""
        return GraphState(
            graph=GraphSpace(
                nodes=tuple(self.nodes.values()),
                edges=tuple(self.edges.values()),
                relation_contracts=self.contracts,
            )
        )

    def log(self, tool, args, result):
        self.tool_log.append(
            {
                "tool": tool,
                "args": args,
                "response_characters": len(json.dumps(result, ensure_ascii=False)),
            }
        )
        return result


def input_fingerprint(context):
    import hashlib
    import json

    data = [
        context.existing.model_dump(mode="json"),
        context.incoming.model_dump(mode="json"),
        context.schema.model_dump(mode="json"),
        sorted(
            {
                (e.id, json.dumps(e.content, ensure_ascii=False, sort_keys=True))
                for e in context.evidence
            }
        ),
    ]
    return hashlib.sha256(
        json.dumps(data, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()
