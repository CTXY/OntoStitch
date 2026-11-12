"""Source dispositions across sequential, accepted workspace batches.

Identity aliases are single-target maps. Structural rewrites are groups and do
not assert pairwise equivalence or propagate SourceReferences to result facts.
The caller owns graph validation, assessment and the serial workspace revision.
"""

from copy import deepcopy
from dataclasses import dataclass, field

from cortex.capabilities.graph_merge.integration.models import Change, Element


def _order(ref):
    return ref.kind, ref.id, ref.alternative_id or ""


def _refs(state):
    result = {Element(kind="edge", id=e.id): e for e in state.graph.edges}
    for node in state.graph.nodes:
        result[Element(kind="node", id=node.id)] = node
        for alternative in node.alternatives:
            result[
                Element(
                    kind="node", id=node.id, alternative_id=alternative.alternative_id
                )
            ] = alternative
    return result


def _resolve(key, aliases):
    seen = set()
    while aliases.get(key, key) != key:
        if key in seen:
            raise ValueError("Identity alias cycle")
        seen.add(key)
        key = aliases[key]
    return key


def _mapped(ref, nodes, edges):
    aliases = nodes if ref.kind == "node" else edges
    return ref.model_copy(update={"id": _resolve(ref.id, aliases)})


@dataclass
class _Group:
    origins: set[Element]
    destinations: set[Element]
    touched: bool = False
    discarded: bool = False
    reasons: list[str] = field(default_factory=list)


class LineageIndex:
    """Retain originals while actions address current workspace IDs.

    A missing current before-handle may name an immutable original, allowing an
    earlier disposition to be corrected (e.g. a deleted edge's true survivor).
    When that original belongs to a many-to-many group, correct the whole group.
    """

    def __init__(self, context, initial_node_map=None, initial_edge_map=None):
        self._originals = {}
        for kind, objects in (("node", context.raw_nodes), ("edge", context.raw_edges)):
            for obj in objects.values():
                ref = Element(kind=kind, id=obj.id)
                self._originals[ref] = obj
                for alternative in getattr(obj, "alternatives", ()):
                    self._originals[
                        Element(
                            kind=kind,
                            id=obj.id,
                            alternative_id=alternative.alternative_id,
                        )
                    ] = alternative
        self.node_map = {key: key for key in context.raw_nodes}
        self.node_map.update(context.physical_map)
        self.node_map.update(initial_node_map or {})
        self.edge_map = {key: key for key in context.raw_edges}
        self.edge_map.update(initial_edge_map or {})
        self._normalize_aliases(self.node_map)
        self._normalize_aliases(self.edge_map)
        self._groups = [
            _Group({ref}, {_mapped(ref, self.node_map, self.edge_map)})
            for ref in self._originals
        ]
        self._retired = set()

    @staticmethod
    def _normalize_aliases(aliases):
        for key in aliases:
            aliases[key] = _resolve(key, aliases)

    def origin_refs(self, kind, object_id):
        """Possible group origins, not a claim each source supports this object."""
        if kind not in {"node", "edge"}:
            raise ValueError("Origin lookup kind must be node or edge")
        aliases = self.node_map if kind == "node" else self.edge_map
        key = _resolve(object_id, aliases)
        groups = [
            g
            for g in self._groups
            if any(r.kind == kind and r.id == key for r in g.destinations)
        ]
        if not groups:
            groups = [
                g
                for g in self._groups
                if any(r.kind == kind and r.id == object_id for r in g.origins)
            ]
        return tuple(sorted({r for g in groups for r in g.origins}, key=_order))

    def observe(
        self,
        before,
        after,
        changes=(),
        *,
        node_aliases=None,
        edge_aliases=None,
        explanation="",
    ):
        """Record one accepted batch atomically; never modify either GraphState.

        Whole-node explicit discard includes its branches. Other removed or
        renamed branches require their own disposition, unless a true alias
        preserves the same branch ID. New objects can have no original object
        predecessor; their SourceReferences remain the graph author's decision.
        """
        previous, current = _refs(before), _refs(after)
        if (current.keys() - previous.keys()) & self._retired:
            raise ValueError("Retired workspace IDs/branch IDs cannot be reused")
        parsed = tuple(Change.model_validate(c) for c in changes)
        groups = deepcopy(self._groups)
        nodes, edges = dict(self.node_map), dict(self.edge_map)
        added_maps = (
            ("node", node_aliases or {}, nodes),
            ("edge", edge_aliases or {}, edges),
        )
        for kind, additions, aliases in added_maps:
            aliases.update(additions)
            self._normalize_aliases(aliases)
            for old, new in additions.items():
                old_ref = Element(kind=kind, id=old)
                new_ref = Element(kind=kind, id=_resolve(new, aliases))
                if (
                    old_ref not in previous
                    and _mapped(old_ref, self.node_map, self.edge_map) != new_ref
                ):
                    raise ValueError(f"Alias source is not a current object: {old}")
                if new_ref not in current:
                    raise ValueError(f"Alias destination does not survive: {new}")
                if (
                    kind == "node"
                    and old_ref in previous
                    and previous[old_ref].node_type != current[new_ref].node_type
                ):
                    raise ValueError("Identity aliases cannot change node type")

        reason = (
            explanation.strip()
            or "Accepted workspace update; unchanged identity retained."
        )
        previous_destinations = {id(g): set(g.destinations) for g in groups}
        # Map existing dispositions, retaining branch identity rather than
        # making a new choice among alternatives.
        for group in groups:
            mapped = {_mapped(r, nodes, edges) for r in group.destinations}
            if mapped != group.destinations:
                group.destinations = mapped
                group.touched = True
                group.reasons.append(reason)

        consumed = set()
        removals_explained = set()
        explicit_discards = set()
        plans = []
        for change in parsed:
            refs = set(change.before)
            if consumed & refs:
                raise ValueError(
                    "Each before-reference may occur in only one batch rewrite"
                )
            consumed |= refs
            destinations = {_mapped(r, nodes, edges) for r in change.after}
            missing = destinations - current.keys()
            if missing:
                raise ValueError(
                    f"Unknown after references: {[r.model_dump() for r in sorted(missing, key=_order)]}"
                )
            current_refs = {r for r in refs if r in previous}
            historical = refs - current_refs
            unknown = historical - self._originals.keys()
            if unknown:
                raise ValueError(
                    f"Unknown before references: {[r.model_dump() for r in sorted(unknown, key=_order)]}"
                )
            if current_refs and historical:
                raise ValueError(
                    "Separate current-object rewrites from immutable-origin corrections"
                )
            selected = []
            for group in groups:
                matches = (
                    (group.origins & historical)
                    if historical
                    else (previous_destinations[id(group)] & current_refs)
                )
                if matches:
                    if historical and not group.origins <= historical:
                        raise ValueError(
                            "Correct all original references of this rewrite group together"
                        )
                    selected.append(group)
            plans.append((change, current_refs, historical, destinations, selected))
            removals_explained |= current_refs
            if not destinations:
                explicit_discards |= current_refs

        for change, _, _, _, selected in plans:
            if (
                len(change.before) > 1
                and len(change.after) > 1
                and any(
                    g in other[4]
                    for g in selected
                    for other in plans
                    if other[0] is not change
                )
            ):
                raise ValueError(
                    "Combine overlapping many-to-many transformations into one complete rewrite group"
                )

        # Every disappearing current object needs an action-level disposition;
        # a true identity alias is an explicit disposition as well.
        for ref in previous.keys() - current.keys():
            mapped = _mapped(ref, nodes, edges)
            parent = Element(kind="node", id=ref.id)
            if mapped in current and mapped != ref:
                continue
            if ref in removals_explained or (
                ref.alternative_id and parent in explicit_discards
            ):
                continue
            raise ValueError(
                f"Removed object/branch has no explicit disposition: {ref.model_dump()}"
            )
        for ref, old in previous.items():
            mapped = _mapped(ref, nodes, edges)
            if (
                ref.alternative_id
                and mapped != ref
                and mapped in current
                and old.overrides != current[mapped].overrides
                and ref not in removals_explained
            ):
                raise ValueError(
                    "Aliased branch content changed; record its explicit branch correspondence"
                )

        # Rewrites are simultaneous. A group touched by multiple independent
        # branch operations advances once using all of them.
        replacements = {}
        for change, current_refs, historical, destinations, selected in plans:
            mapped_before = {_mapped(r, nodes, edges) for r in current_refs}
            for group in selected:
                entry = replacements.setdefault(
                    id(group), [group, set(), set(), [], False]
                )
                entry[1].update(group.destinations if historical else mapped_before)
                entry[2].update(destinations)
                entry[3].append(change.explanation)
                entry[4] |= not destinations
            # Many-to-many correspondence stays a collective group. Do not turn
            # it into independently claimed source-to-result pairs.
            if len(change.before) > 1 and len(change.after) > 1 and selected:
                roots = set().union(*(g.origins for g in selected))
                remaining = set().union(
                    *(g.destinations - mapped_before for g in selected)
                )
                for group in selected:
                    replacements.pop(id(group), None)
                    if group in groups:
                        groups.remove(group)
                merged = _Group(
                    roots, remaining | destinations, True, False, [change.explanation]
                )
                groups.append(merged)

        for group, removed, destinations, reasons, discarded in replacements.values():
            group.destinations = (group.destinations - removed) | destinations
            group.touched = True
            group.discarded = not group.destinations and discarded
            group.reasons.extend(reasons)

        for group in groups:
            # Discard of a whole current conflict explicitly discards its
            # original branch dispositions too.
            removed_children = {
                r
                for r in group.destinations
                if r.alternative_id
                and Element(kind="node", id=r.id) in explicit_discards
            }
            if removed_children:
                group.destinations -= removed_children
                group.touched = True
                group.discarded = not group.destinations
                group.reasons.append(reason)
            changed = any(
                r in previous and r in current and previous[r] != current[r]
                for r in group.destinations
            )
            if changed:
                group.touched = True
                group.reasons.append(reason)
            missing = group.destinations - current.keys()
            if missing:
                raise ValueError(
                    f"Original disposition still points to removed objects: {[r.model_dump() for r in sorted(missing, key=_order)]}"
                )
            if not group.destinations and not group.discarded:
                raise ValueError("Empty source disposition requires explicit discard")

        self.node_map, self.edge_map, self._groups = nodes, edges, groups
        self._retired |= previous.keys() - current.keys()

    def source_changes(self, final_state):
        """Export immutable-original before refs and surviving final after refs."""
        available = _refs(final_state)
        result = []
        for group in self._groups:
            if group.destinations - available.keys():
                raise ValueError(
                    "Final graph differs from observed lineage; surviving reference missing"
                )
            if not group.destinations and not group.discarded:
                raise ValueError("Empty source disposition requires explicit discard")
            if group.touched:
                result.append(
                    Change(
                        before=tuple(sorted(group.origins, key=_order)),
                        after=tuple(sorted(group.destinations, key=_order)),
                        explanation="\n".join(dict.fromkeys(group.reasons))
                        or "Accepted source disposition.",
                    )
                )
        return tuple(sorted(result, key=lambda c: tuple(_order(r) for r in c.before)))

    def snapshot(self):
        return {
            "version": 1,
            "node_map": dict(self.node_map),
            "edge_map": dict(self.edge_map),
            "retired": [
                r.model_dump(mode="json") for r in sorted(self._retired, key=_order)
            ],
            "groups": [
                {
                    "origins": [
                        r.model_dump(mode="json") for r in sorted(g.origins, key=_order)
                    ],
                    "destinations": [
                        r.model_dump(mode="json")
                        for r in sorted(g.destinations, key=_order)
                    ],
                    "touched": g.touched,
                    "discarded": g.discarded,
                    "reasons": list(g.reasons),
                }
                for g in self._groups
            ],
        }

    @classmethod
    def restore(cls, context, snapshot):
        if snapshot.get("version") != 1:
            raise ValueError("Unsupported lineage snapshot version")
        result = cls(context, snapshot["node_map"], snapshot["edge_map"])
        result._groups = [
            _Group(
                {Element.model_validate(r) for r in g["origins"]},
                {Element.model_validate(r) for r in g["destinations"]},
                g["touched"],
                g["discarded"],
                list(g["reasons"]),
            )
            for g in snapshot["groups"]
        ]
        roots = [ref for g in result._groups for ref in g.origins]
        if len(set(roots)) != len(roots) or set(roots) != result._originals.keys():
            raise ValueError(
                "Lineage snapshot does not cover these immutable originals exactly"
            )
        result._retired = {
            Element.model_validate(r) for r in snapshot.get("retired", [])
        }
        return result
