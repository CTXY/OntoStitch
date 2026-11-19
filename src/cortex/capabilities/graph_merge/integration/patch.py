"""One task-relative semantic patch, compiled from a fixed accepted baseline."""
from dataclasses import dataclass

from pydantic import Field, model_validator

from cortex.application.ontology_graph import GraphManager
from cortex.capabilities.graph_merge.integration.batch import ActiveBatch, BatchResult
from cortex.capabilities.graph_merge.integration.compiler import deduplicate_edges
from cortex.capabilities.graph_merge.integration.context import PHYSICAL, remap_edge, remap_node
from cortex.capabilities.graph_merge.integration.evidence_refs import evidence_sources, EvidencePresentation
from cortex.capabilities.graph_merge.integration.models import Change, Element, Model, SourceDisposition
from cortex.capabilities.graph_merge.integration.proposal_scope import ProposalScope
from cortex.capabilities.graph_merge.integration.relation_edits import EvidenceAlternative, RelationPatch, submit_patch
from cortex.schema import ConflictPoint, GraphSpace, GraphState, NodeContent


class IdentityIntegration(Model):
    owner_id: str
    member_ids: tuple[str, ...] = Field(min_length=2)
    content: NodeContent | None = None
    alternatives: tuple[EvidenceAlternative, ...] = ()
    common_evidence_ids: tuple[str, ...] = ()
    conflict_points: tuple[ConflictPoint, ...] = ()
    branch_dispositions: tuple[SourceDisposition, ...] = ()
    explanation: str = Field(min_length=1)

    @model_validator(mode='after')
    def complete_result(self):
        if (self.content is None) == (not self.alternatives):
            raise ValueError('Supply complete normal content or at least two complete alternatives')
        if self.alternatives and (len(self.alternatives) < 2 or not self.conflict_points):
            raise ValueError('Conflict integration requires two alternatives and conflict_points')
        if len(set(self.member_ids)) != len(self.member_ids) or self.owner_id not in self.member_ids:
            raise ValueError('Identity members must be distinct and include their planned owner')
        return self


class MergePatch(RelationPatch):
    """The complete current draft. Replacing it never inherits an earlier draft."""
    integrate_groups: tuple[IdentityIntegration, ...] = ()


@dataclass
class CompiledPatch:
    state: GraphState
    integrated: GraphState
    integration_changes: tuple[Change, ...]
    changes: tuple[Change, ...]
    node_aliases: dict[str, str]
    edge_aliases: dict[str, str]
    extensions: list[dict]
    batch: ActiveBatch


def identity_obligations(base, aliases):
    nodes = {n.id: n for n in base.graph.nodes}
    groups = {}
    for member, owner in (aliases or {}).items():
        if member in nodes and nodes[member].node_type not in PHYSICAL:
            groups.setdefault(owner, set()).add(member)
    return [{'owner_id': owner, 'member_ids': sorted(members | {owner})}
            for owner, members in sorted(groups.items()) if members - {owner}]


def _integrate(base, integrations, obligations, context):
    nodes = {n.id: n for n in base.graph.nodes}
    expected = {g['owner_id']: set(g['member_ids']) for g in obligations}
    found = {g.owner_id: set(g.member_ids) for g in integrations}
    if len(found) != len(integrations) or found != expected:
        raise ValueError(f'Complete each planned identity group exactly once: {obligations}')
    aliases, changes, branch_map, owners = {}, [], {}, {}
    used = set()
    for integration in integrations:
        if used & set(integration.member_ids):
            raise ValueError('Identity groups overlap')
        used.update(integration.member_ids)
        members = [nodes[key] for key in integration.member_ids]
        owner = nodes[integration.owner_id]
        if len({m.node_type for m in members}) != 1 or owner.node_type in PHYSICAL:
            raise ValueError('Integrations require semantic nodes of the same type')
        old_branches = {(n.id, a.alternative_id): a for n in members for a in n.alternatives}
        support = {key: value.sources for key, value in old_branches.items()}
        if integration.alternatives:
            support.update({(n.id, None): (getattr(n, 'common_sources', ()) if n.alternatives else n.sources)
                            for n in members if not n.alternatives or getattr(n, 'common_sources', ())})
        dispositions = {(d.source_node_id, d.source_alternative_id): d for d in integration.branch_dispositions}
        if len(dispositions) != len(integration.branch_dispositions) or dispositions.keys() != support.keys():
            raise ValueError(f'Each member/common scope and original branch needs a disposition: {list(support)}')
        new_ids = {a.alternative_id for a in integration.alternatives}
        if len(new_ids) != len(integration.alternatives):
            raise ValueError('New alternative IDs must be distinct')
        common = evidence_sources(context, integration.common_evidence_ids)
        if integration.content is not None:
            kept_sources = set(common)
            for n in members:
                if not n.alternatives:
                    kept_sources.update(n.sources)
                kept_sources.update(getattr(n, 'common_sources', ()))
            for key, disposition in dispositions.items():
                if disposition.target_alternative_ids:
                    raise ValueError('Normal integration has no destination alternative IDs; use retained_in_common')
                if disposition.retained_in_common:
                    kept_sources.update(support[key])
            output = owner.without_conflict().model_copy(update={
                **integration.content.model_dump(mode='json'),
                'sources': tuple(sorted(kept_sources, key=lambda s: s.model_dump_json()))})
        else:
            candidates = []
            for alternative in integration.alternatives:
                sources = set(evidence_sources(context, alternative.evidence_ids))
                for key, disposition in dispositions.items():
                    if alternative.alternative_id in disposition.target_alternative_ids:
                        sources.update(support[key])
                candidates.append((alternative.alternative_id, alternative.content,
                                   tuple(sorted(sources, key=lambda s: s.model_dump_json()))))
            for key, disposition in dispositions.items():
                if disposition.retained_in_common:
                    common = tuple(dict.fromkeys((*common, *support[key])))
            output, _ = GraphManager.compact_conflict(owner, candidates,
                common_sources=common, conflict_points=integration.conflict_points,
                consolidation_status='semantic_conflict')
        owners[owner.id] = output
        aliases.update({n.id: owner.id for n in members})
        changes.append(Change(before=tuple(Element(kind='node', id=n.id) for n in members),
                              after=(Element(kind='node', id=owner.id),), explanation=integration.explanation))
        for key, disposition in dispositions.items():
            if key[1] is None and not disposition.retained_in_common and not disposition.target_alternative_ids:
                raise ValueError('An accepted member/common assertion needs a preservation destination')
            if set(disposition.target_alternative_ids) - new_ids:
                raise ValueError(f'Unknown destination branches for {key}')
            destinations = tuple(Element(kind='node', id=owner.id, alternative_id=k)
                                 for k in disposition.target_alternative_ids)
            if disposition.retained_in_common:
                destinations = (Element(kind='node', id=owner.id),)
            branch_map[key] = set(new_ids if disposition.retained_in_common else disposition.target_alternative_ids)
            if key[1] is not None:
                changes.append(Change(before=(Element(kind='node', id=key[0], alternative_id=key[1]),),
                                      after=destinations, explanation=disposition.explanation))
    new_nodes = [remap_node(owners.get(n.id, n), aliases) for n in base.graph.nodes
                 if n.id not in aliases or aliases[n.id] == n.id]
    new_edges = []
    for edge in base.graph.edges:
        updated = remap_edge(edge, aliases)
        original = nodes[edge.from_node_id]
        if original.id in aliases and (original.alternatives or owners[aliases[original.id]].alternatives):
            source_ids = (edge.properties.get('alternative_ids') or [a.alternative_id for a in original.alternatives]) if original.alternatives else [None]
            scope = set().union(*(branch_map[(original.id, key)] for key in source_ids))
            target = owners[aliases[original.id]]
            if target.alternatives and not scope:
                changes.append(Change(before=(Element(kind='edge', id=edge.id),), after=(),
                                      explanation='All source branches of this edge were explicitly retired.'))
                continue
            props = dict(updated.properties)
            if target.alternatives:
                props['alternative_ids'] = sorted(scope)
            else:
                retained = any(branch_map[(original.id, key)] or next(
                    d.retained_in_common for g in integrations for d in g.branch_dispositions
                    if (d.source_node_id, d.source_alternative_id) == (original.id, key)) for key in source_ids)
                if not retained:
                    changes.append(Change(before=(Element(kind='edge', id=edge.id),), after=(),
                                          explanation='The edge only applied to explicitly discarded branches.'))
                    continue
                props.pop('alternative_ids', None)
            updated = updated.model_copy(update={'properties': props})
        new_edges.append(updated)
    edges, edge_map = deduplicate_edges(new_edges)
    new_nodes = [remap_node(n, {}, edge_map) for n in new_nodes]
    return GraphState(graph=GraphSpace(nodes=tuple(new_nodes), edges=tuple(edges.values()),
                                     relation_contracts=base.graph.relation_contracts)), changes, aliases, edge_map


def compile_patch(context, base, patch, *, targets, aliases=None, payload=None):
    """Atomically expose a candidate; failed compilation preserves prior views."""
    old_candidate = context.candidate
    old_batch = getattr(context, 'active_batch', None)
    try:
        return _compile_patch(context, base, patch, targets=targets, aliases=aliases, payload=payload)
    except BaseException:
        context.candidate, context.active_batch = old_candidate, old_batch
        raise


def _compile_patch(context, base, patch, *, targets, aliases=None, payload=None):
    """Build fresh graph/scope each time; a rejected/replaced draft leaves no writes."""
    if patch.failure:
        from cortex.capabilities.graph_merge.integration.engine import IntegrationIncomplete
        raise IntegrationIncomplete(patch.failure)
    obligations = identity_obligations(base, aliases)
    original_nodes = {n.id: n for n in base.graph.nodes}
    integrations = list(patch.integrate_groups)
    existing_conflicts = [c for c in patch.conflicts if c.node_id in original_nodes]
    source_payload = dict(payload or {})
    if existing_conflicts:
        # A same-owner branch rewrite uses the same preservation compiler as an
        # identity integration, without granting a new semantic identity alias.
        scope_batch = ActiveBatch(context, base, targets=targets)
        context.active_batch, context.candidate = scope_batch, base
        scope = ProposalScope(context, source_payload, EvidencePresentation(context, source_payload.get('evidence', [])))
        scope.visible.update(getattr(context, 'task_visible_ids', ()))
        admitted = scope.prepare(BatchResult.model_validate({'edits': [
            {'operation': 'update_node', 'node': {'id': c.node_id}} for c in existing_conflicts]}))
        targets = set(targets) | admitted
        for conflict in existing_conflicts:
            obligations.append({'owner_id': conflict.node_id, 'member_ids': [conflict.node_id]})
            # The public group operation requires >=2 members. This trusted
            # translation is a validated single-owner conflict operation.
            integrations.append(IdentityIntegration.model_construct(
                owner_id=conflict.node_id, member_ids=(conflict.node_id,), content=None,
                alternatives=conflict.alternatives, common_evidence_ids=conflict.common_evidence_ids,
                conflict_points=conflict.conflict_points, branch_dispositions=conflict.branch_dispositions,
                explanation='Rewrite complete branches with explicit source and relation dispositions.'))
    integrated, changes, node_map, edge_map = _integrate(base, integrations, obligations, context)
    live_targets = {node_map.get(key, key) for key in targets}
    batch = ActiveBatch(context, integrated, targets=live_targets)
    if existing_conflicts and admitted:
        batch.scope_events.append({'ids': sorted(admitted), 'reason': 'Explicit coupled conflict rewrite'})
    context.active_batch = batch
    context.candidate = integrated
    # Scope is reconstructed from the fixed base; reads only supply context.
    scope = ProposalScope(context, source_payload, EvidencePresentation(context, source_payload.get('evidence', [])))
    scope.visible.update(getattr(context, 'task_visible_ids', ()))
    batch.prepare_submission = scope.prepare
    local_data = patch.model_dump(mode='json', exclude={'integrate_groups'}, exclude_unset=True)
    local_data['conflicts'] = [c.model_dump(mode='json', exclude_unset=True) for c in patch.conflicts if c.node_id not in original_nodes]
    if any(c.branch_dispositions for c in patch.conflicts if c.node_id not in original_nodes):
        raise ValueError('A newly created conflict owner has no original branch dispositions')
    local = RelationPatch.model_validate(local_data)
    result = submit_patch(context, local)
    final_map = {key: batch.edge_aliases.get(value, value) for key, value in edge_map.items()}
    return CompiledPatch(batch.preview, integrated, tuple(changes), result.changes, node_map, final_map,
                         batch.scope_events, batch)
