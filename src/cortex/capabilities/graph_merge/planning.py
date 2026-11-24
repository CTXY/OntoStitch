"""Plan complete node groups and indivisible local relation checks."""
from cortex.capabilities.graph_merge.graph.operations import PHYSICAL
from cortex.capabilities.graph_merge.runtime.errors import IntegrationIncomplete

def _nodes(state):
    return {n.id: n for n in state.graph.nodes}

def active_identity_projection(current, aliases, lineage):
    """Project live members only; retain recorded earlier structural rewrites.

    Identity is already Agent-confirmed. Provenance here only verifies why an
    absent member needs no second physical projection; it establishes no match.
    The subsequent Agent still compares the complete immutable matched group.
    """
    nodes = _nodes(current)
    active = {}
    for source, target in aliases.items():
        if target not in nodes:
            raise IntegrationIncomplete(
                f"Matched canonical {target} was retired; rebuild the unpublished plan"
            )
        if source in nodes:
            active[source] = target
        elif not any(
            r.kind == "node" and r.id == source
            for r in lineage.origin_refs("node", target)
        ):
            raise IntegrationIncomplete(
                f"Matched member {source} is absent without a recorded rewrite to {target}"
            )
    return active

def identity_task_projection(current, aliases, lineage):
    """Keep ordinary aliases; route rewritten groups to explicit Agent review.

    Provenance locates current assertions, but does not establish a new identity.
    A stale group therefore receives no mechanical aliases. The Writer and its
    independent reviewer compare complete archived groups with all live outputs.
    """
    try:
        active = active_identity_projection(current, aliases, lineage)
        return active, set(active.values()), []
    except IntegrationIncomplete:
        nodes = _nodes(current)
        origins = {
            key: {r.id for r in lineage.origin_refs("node", key) if r.kind == "node"}
            for key in nodes
        }
        locations = {}
        for source in sorted(set(aliases) | set(aliases.values())):
            live = {key for key, refs in origins.items() if source in refs}
            if source in nodes:
                live.add(source)
            if not live:
                raise  # An unrepresented/discarded source needs a new plan.
            locations[source] = sorted(live)
        import json

        feedback = [
            "An earlier accepted rewrite changed the live objects of this archived "
            "matched group. These provenance locations do not assert new identity: "
            + json.dumps(locations, sort_keys=True)
            + ". Compare complete current contents with the archived group. Preserve "
            "accepted rewrites and make explicit sourced edits/dispositions for any "
            "remaining integration. A group already represented by one live object "
            "needs content review without recreating retired copies. If the current "
            "meanings reveal an identity counterexample, report it."
        ]
        return {}, set().union(*(set(v) for v in locations.values())), feedback

def _signal_targets(check, state):
    edges = {e.id: e for e in state.graph.edges}
    keys = set(check.get('node_ids', ())) | set(check.get('edge_ids', ()))
    keys |= {x.get('concept') for x in check.get('members', ())}
    for eid in check.get('edge_ids', ()):
        if eid in edges:
            keys.update((edges[eid].from_node_id, edges[eid].to_node_id))
    if check.get('kind') == 'source_selection_variants':
        operands = set(check.get('node_ids', ()))
        keys.update(e.from_node_id for e in edges.values()
                    if e.edge_type == 'applies_to' and e.to_node_id in operands)
    return {k for k in keys if k in _nodes(state) or k in edges}

def review_batches(state, *, targets=None, checks=(), budget=14000, signals_only=False):
    """Bound root definitions, keeping each supplied inspection indivisible.

    Context neighbours are retrieved separately and are not writable by default.
    No domain topic, semantic conclusion or fixed case partition is encoded here.
    """
    nodes = _nodes(state)
    roots = set(
        targets
        if targets is not None
        else (k for k, n in nodes.items() if n.node_type not in PHYSICAL)
    )
    units = [(set(_signal_targets(c, state)), [c]) for c in checks]
    covered = set().union(*(u[0] for u in units)) if units else set()
    if not signals_only:
        units.extend(({k}, []) for k in sorted(roots - covered))
    batches, keys, findings, used = [], set(), [], 0
    for ids, cs in units:
        cost = sum(len(nodes[k].model_dump_json()) for k in ids - keys if k in nodes)
        if keys and used + cost > budget:
            batches.append((keys, findings))
            keys, findings, used = set(), [], 0
        keys |= ids
        findings.extend(cs)
        used += cost
    if keys:
        batches.append((keys, findings))
    return batches
