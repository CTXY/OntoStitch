"""Check exact source coverage without inferring or propagating business support."""


class SourceCoverageError(ValueError):
    """Preserve the existing error text and expose actionable source/owner data."""
    def __init__(self, message, *, before, after, missing):
        super().__init__(message)
        self.before = before
        self.after = after
        self.missing_sources = missing


def element_sources(obj, alternative_id=None):
    alternatives = getattr(obj, "alternatives", ())
    if alternative_id:
        return set(
            next(a for a in alternatives if a.alternative_id == alternative_id).sources
        )
    return set(obj.sources) | {s for a in alternatives for s in a.sources}


def validate_rewrite_sources(context, proposal, preview):
    """Nonempty replacements retain original references across their explicit group.

    This is group-level coverage, not pairwise equivalence or a claim that each
    source supports every result. Empty-after discards remain explicit decisions.
    """
    nodes = {n.id: n for n in preview.state.graph.nodes}
    edges = {e.id: e for e in preview.state.graph.edges}
    rewritten = {(ref.kind, ref.id) for c in proposal.changes for ref in c.before}
    for kind, originals, results, aliases in (
        ("node", context.raw_nodes, nodes, preview.node_map),
        ("edge", context.raw_edges, edges, preview.edge_map),
    ):
        for key, original in originals.items():
            target = aliases.get(key, key)
            if (kind, key) in rewritten or target not in results:
                continue
            missing = element_sources(original) - element_sources(results[target])
            if missing:
                from cortex.capabilities.graph_merge.integration.models import Element
                raise SourceCoverageError(
                    f"Direct {kind} mapping {key} loses exact SourceReferences; preserve them or record an explicit source disposition.",
                    before=(Element(kind=kind, id=key),),
                    after=(Element(kind=kind, id=target),), missing=missing,
                )
    for index, change in enumerate(proposal.changes):
        if not change.after:
            continue
        before = set()
        after = set()
        for ref in change.before:
            objects = context.raw_nodes if ref.kind == "node" else context.raw_edges
            before.update(element_sources(objects[ref.id], ref.alternative_id))
        for ref in change.after:
            objects = nodes if ref.kind == "node" else edges
            aliases = preview.node_map if ref.kind == "node" else preview.edge_map
            after.update(
                element_sources(
                    objects[aliases.get(ref.id, ref.id)], ref.alternative_id
                )
            )
        missing = before - after
        if missing:
            raise SourceCoverageError(
                f"changes[{index}] replacement loses exact SourceReferences: "
                f"{[s.model_dump(mode='json') for s in sorted(missing, key=lambda s: s.model_dump_json())]}. "
                "Name the actual surviving assertions and preserve their relevant sources; "
                "do not propagate sources to unrelated facts.",
                before=change.before, after=change.after, missing=missing,
            )
