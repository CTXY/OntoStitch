"""One task-relative semantic patch, compiled from a fixed accepted baseline."""
from dataclasses import dataclass

from pydantic import Field, model_validator

from cortex.application.ontology_graph import GraphManager
from cortex.capabilities.graph_merge.integration.batch import ActiveBatch, BatchResult
from cortex.capabilities.graph_merge.integration.compiler import deduplicate_edges
from cortex.capabilities.graph_merge.integration.context import PHYSICAL, remap_edge, remap_node
from cortex.capabilities.graph_merge.integration.evidence_refs import evidence_sources, EvidencePresentation
from cortex.capabilities.graph_merge.integration.models import Change, Element, Model
from cortex.capabilities.graph_merge.integration.proposal_scope import ProposalScope
from cortex.capabilities.graph_merge.integration.relation_edits import EvidenceAlternative, RelationPatch, submit_patch
from cortex.capabilities.graph_merge.integration.source_units import SourceRoute, PatchContractError, validate_source_routes
from cortex.schema import ConflictPoint, GraphSpace, GraphState, NodeContent


class IdentityIntegration(Model):
    owner_id: str
    member_ids: tuple[str, ...] = Field(min_length=2)
    content: NodeContent | None = None
    alternatives: tuple[EvidenceAlternative, ...] = ()
    common_evidence_ids: tuple[str, ...] = ()
    conflict_points: tuple[ConflictPoint, ...] = ()
    source_routes: tuple[SourceRoute, ...] = ()
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
    if len(expected) != len(obligations):
        raise PatchContractError('duplicate_owner_obligations', required_action='One operation must own each result owner.')
    found = {g.owner_id: set(g.member_ids) for g in integrations}
    if len(found) != len(integrations) or found != expected:
        raise ValueError(f'Complete each planned identity group exactly once: {obligations}')
    aliases, changes, branch_map, owners, retained_map = {}, [], {}, {}, {}
    used = set()
    for integration in integrations:
        if used & set(integration.member_ids):
            raise ValueError('Identity groups overlap')
        used.update(integration.member_ids)
        members = [nodes[key] for key in integration.member_ids]
        owner = nodes[integration.owner_id]
        if len({m.node_type for m in members}) != 1 or owner.node_type in PHYSICAL:
            raise ValueError('Integrations require semantic nodes of the same type')
        routes = validate_source_routes(base, integration)
        new_ids = {a.alternative_id for a in integration.alternatives}
        if len(new_ids) != len(integration.alternatives):
            raise ValueError('New alternative IDs must be distinct')
        common = evidence_sources(context, integration.common_evidence_ids)
        if integration.content is not None:
            kept_sources = set(common)
            for unit, route in routes.values():
                if route.destination.kind == 'result':
                    kept_sources.update(unit['_sources'])
            output = owner.without_conflict().model_copy(update={
                **integration.content.model_dump(mode='json'),
                'sources': tuple(sorted(kept_sources, key=lambda s: s.model_dump_json()))})
        else:
            candidates = []
            for alternative in integration.alternatives:
                if not alternative.evidence_ids and not any(
                    route.destination.kind == 'common' or alternative.alternative_id in route.destination.alternative_ids
                    for unit, route in routes.values()
                ):
                    raise PatchContractError('unsupported_result_alternative', owner_id=owner.id,
                        alternative_id=alternative.alternative_id,
                        required_action='Retain an input unit in this branch or cite known supporting evidence.')
                sources = set(evidence_sources(context, alternative.evidence_ids))
                for unit, route in routes.values():
                    if alternative.alternative_id in route.destination.alternative_ids:
                        sources.update(unit['_sources'])
                candidates.append((alternative.alternative_id, alternative.content,
                                   tuple(sorted(sources, key=lambda s: s.model_dump_json()))))
            for unit, route in routes.values():
                if route.destination.kind == 'common':
                    common = tuple(dict.fromkeys((*common, *unit['_sources'])))
            output, _ = GraphManager.compact_conflict(owner, candidates,
                common_sources=common, conflict_points=integration.conflict_points,
                consolidation_status='semantic_conflict')
        owners[owner.id] = output
        aliases.update({n.id: owner.id for n in members})
        changes.append(Change(before=tuple(Element(kind='node', id=n.id) for n in members),
                              after=(Element(kind='node', id=owner.id),), explanation=integration.explanation))
        for key, (unit, route) in routes.items():
            dest = route.destination
            destinations = tuple(Element(kind='node', id=owner.id, alternative_id=k)
                                 for k in dest.alternative_ids)
            if dest.kind in {'common', 'result'}:
                destinations = (Element(kind='node', id=owner.id),)
            branch_map[key] = set(new_ids if dest.kind == 'common' else dest.alternative_ids)
            retained_map[key] = dest.kind == 'result'
            if key[1] is not None:
                changes.append(Change(before=(Element(kind='node', id=key[0], alternative_id=key[1]),),
                                      after=destinations, explanation=route.explanation))
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
                retained = any(retained_map[(original.id, key)] for key in source_ids)
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



def validate_operation_boundaries(patch, obligations):
    """No single-owner rewrite may replace or overwrite a planned identity group."""
    planned = {key for g in obligations for key in g['member_ids']}
    owners = [g.owner_id for g in patch.integrate_groups]
    conflicts = [c.node_id for c in patch.conflicts]
    resolutions = [r.node_id for r in patch.resolutions]
    for name, ids in [('integrate_groups', owners), ('conflicts', conflicts), ('resolutions', resolutions)]:
        if len(ids) != len(set(ids)):
            raise PatchContractError('duplicate_operation_owner', path=name, owners=ids)
    wrong = sorted(planned & (set(conflicts) | set(resolutions)))
    if wrong:
        raise PatchContractError('identity_operation_required', path='conflicts/resolutions', node_ids=wrong,
            required_action='Use integrate_groups for the complete planned member set; single-owner operations cannot replace identity integration.')
    overlap = (set(owners) & (set(conflicts) | set(resolutions))) | (set(conflicts) & set(resolutions))
    if overlap:
        raise PatchContractError('overlapping_owner_operations', node_ids=sorted(overlap))



def validate_owner_edits(patch, obligations):
    planned = {key for g in obligations for key in g['member_ids']}
    owners = [g.owner_id for g in patch.integrate_groups]
    conflicts = [c.node_id for c in patch.conflicts]
    resolutions = [r.node_id for r in patch.resolutions]
    rewritten = planned | set(owners) | set(conflicts) | set(resolutions)
    updates = rewritten & {n.id for n in patch.node_updates}
    retired = rewritten & {r.id for retirement in patch.retirements for r in retirement.before if r.kind == 'node'}
    if updates or retired:
        raise PatchContractError('contradictory_owner_edit', node_ids=sorted(updates | retired),
            required_action='Express complete owner content in its one integration/conflict/resolution operation, not node_updates or retirements.')


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
    validate_operation_boundaries(patch, obligations)
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
                conflict_points=conflict.conflict_points, source_routes=conflict.source_routes,
                explanation='Rewrite complete branches with explicit source and relation dispositions.'))
    integrated, changes, node_map, edge_map = _integrate(base, integrations, obligations, context)
    validate_owner_edits(patch, obligations)
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
    if any(not a.evidence_ids for c in patch.conflicts if c.node_id not in original_nodes for a in c.alternatives):
        raise PatchContractError('new_conflict_requires_evidence', required_action='A new owner has no input units; cite supporting evidence for every complete branch.')
    local_data['conflicts'] = [c.model_dump(mode='json', exclude_unset=True) for c in patch.conflicts if c.node_id not in original_nodes]
    if any(c.source_routes for c in patch.conflicts if c.node_id not in original_nodes):
        raise ValueError('A newly created conflict owner has no original source routes')
    local = RelationPatch.model_validate(local_data)
    result = submit_patch(context, local)
    final_map = {key: batch.edge_aliases.get(value, value) for key, value in edge_map.items()}
    return CompiledPatch(batch.preview, integrated, tuple(changes), result.changes, node_map, final_map,
                         batch.scope_events, batch)
