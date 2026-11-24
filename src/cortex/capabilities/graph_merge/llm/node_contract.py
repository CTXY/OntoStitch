"""Narrow, group-local contracts for node consolidation and its assessment.

The two public result schemas contain no graph-edit operations. Relation work is
returned separately so the controller can persist and reconcile it explicitly.
"""

from collections import Counter
from importlib.resources import files
from typing import Literal

from pydantic import Field, field_validator, model_validator

from cortex.capabilities.graph_merge.integration.models import Model
from cortex.capabilities.graph_merge.integration.patch import IdentityIntegration, MergePatch, LocalRelationRemoval
from cortex.capabilities.graph_merge.integration.review import Review, ReviewReference
from cortex.schema.semantic_validation import validate_semantic_properties
from cortex.schema.references import property_reference_ids
from cortex.capabilities.graph_merge.integration.source_units import PatchContractError

NODE_CONTRACT_VERSION = 'node-local-v6-partition'
NODE_TYPES = ('concept', 'metric', 'event', 'generation_rule')
NODE_ROLES = ('writer', 'assessment')


def node_role(role):
    """Accept controller phase names without creating a second prompt policy."""
    value = {'consolidate': 'writer', 'node_assess': 'assessment'}.get(role, role)
    if value not in NODE_ROLES:
        raise ValueError(f'Unknown node role: {role}')
    return value


def node_prompt_modules(role, kind):
    role = node_role(role)
    if kind not in NODE_TYPES:
        raise ValueError(f'Unsupported local node type: {kind}')
    return ('common-fields.en.md', f'{role}.system.en.md',
            f'{role}-output-contract.en.md', f'types/{kind}.en.md')


def node_system(role, kind):
    """Shared semantics and role/format first; unique type guidance always last."""
    root = files('cortex.capabilities.graph_merge.llm.prompts').joinpath('node_local')
    return '\n\n'.join(root.joinpath(name).read_text().rstrip()
                       for name in node_prompt_modules(role, kind)) + '\n'


class RelationFollowup(Model):
    edge_ids: tuple[str, ...] = Field(min_length=1,
        description='Stable original edge IDs displayed in the fixed group input.')
    problem: str = Field(min_length=1)
    required_outcome: str = Field(min_length=1)

    @field_validator('edge_ids')
    @classmethod
    def unique_edges(cls, values):
        if any(not value.strip() for value in values) or len(set(values)) != len(values):
            raise ValueError('Followup edge IDs must be nonempty and distinct')
        return values

    @field_validator('problem', 'required_outcome')
    @classmethod
    def meaningful_text(cls, value):
        if not value.strip():
            raise ValueError('Relation followup text must be nonempty')
        return value


class NodeWriterResult(Model):
    failure: str | None = Field(default=None,
        description='Omit or JSON null on success; a concrete inability to integrate otherwise.')
    integrate_groups: tuple[IdentityIntegration, ...] = Field(default=(), description="Complete, disjoint subgroup integrations inside the one fixed input group.")
    keep_separate_ids: tuple[str, ...] = Field(default=(), description='Original group members retained as standalone nodes, never deleted. Every other original member belongs to exactly one integration subgroup.')
    relation_removals: tuple[LocalRelationRemoval, ...] = Field(default=(), description='Explicitly remove original group-internal relations, with a reason checked by Assessment.')
    relation_followups: tuple[RelationFollowup, ...] = ()
    explanation: str = Field(min_length=1)

    @model_validator(mode='after')
    def complete_local_result(self):
        # Reuse production failure/null and integration validators.
        MergePatch.model_validate(self.model_dump(exclude={'relation_followups'}))
        if self.failure is None and not self.integrate_groups and not self.keep_separate_ids:
            raise ValueError('Cover every original member with complete integration subgroups or explicit singleton IDs')
        if len(set(self.keep_separate_ids)) != len(self.keep_separate_ids):
            raise ValueError('keep_separate_ids must be distinct')
        removed = {row.edge_id for row in self.relation_removals}
        if any(removed & set(row.edge_ids) for row in self.relation_followups):
            raise ValueError('A removed relation cannot also be handed off as a relation followup')
        if self.failure is not None and (self.integrate_groups or self.relation_followups or self.keep_separate_ids or self.relation_removals):
            raise ValueError('A concrete failure cannot contain integrations or relation followups')
        return self


class NodeReviewReference(ReviewReference):
    view: Literal['before', 'candidate'] = 'candidate'


class NodeReviewIssue(Model):
    refs: tuple[NodeReviewReference, ...] = Field(min_length=1)
    basis: tuple[NodeReviewReference, ...] = ()
    problem: str = Field(min_length=1)
    required_outcome: str = Field(min_length=1)


class NodeAssessmentResult(Model):
    verdict: Literal['accept', 'revise', 'needs_context']
    issues: tuple[NodeReviewIssue, ...] = ()
    relation_followups: tuple[RelationFollowup, ...] = ()

    @model_validator(mode='after')
    def coherent_review(self):
        Review.model_validate(self.model_dump(exclude={'relation_followups'}))
        return self


def node_output_model(role):
    return NodeWriterResult if node_role(role) == 'writer' else NodeAssessmentResult


def canonical_node_result(role, value):
    """Normalize a current narrow draft without expanding legacy empty operations."""
    return node_output_model(role).model_validate(value).model_dump(mode='json', exclude_unset=True)


def _relation_rows(value):
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        return [row for key in ('outgoing', 'incoming') for row in value.get(key, [])]
    return []


def local_original_edge_ids(payload):
    """Assessment followups keep original handles even after edge deduplication."""
    relations = payload.get('before_relations', payload.get('relations', []))
    return {row['id'] for row in _relation_rows(relations) if isinstance(row, dict) and 'id' in row}


def local_evidence_ids(payload):
    """Only displayed attribution IDs grant evidence-read or citation permission."""
    if 'allowed_evidence_ids' in payload:
        return set(payload['allowed_evidence_ids'])
    found = set()

    def walk(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key in {'evidence_ids', 'common_evidence_ids', 'allowed_evidence_ids'}:
                    if isinstance(item, (list, tuple)):
                        found.update(v for v in item if isinstance(v, str))
                elif key not in {'revision', 'draft'}:
                    walk(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                walk(item)

    walk(payload)
    return found


def build_node_read_tools(context, payload):
    """Give node roles one ID-scoped evidence tool and no graph inspection."""
    from langchain_core.tools import tool

    from cortex.capabilities.graph_merge.integration.evidence_refs import EvidencePresentation

    evidence = EvidencePresentation(context)
    allowed = local_evidence_ids(payload)

    @tool
    def read_evidence(ids: list[str]) -> dict:
        """Read original Question, Gold SQL and evidence only by IDs displayed in this local group."""
        unknown = set(ids) - allowed
        if unknown:
            raise ValueError(f'Evidence IDs outside this local task are not readable: {sorted(unknown)}')
        if not ids:
            raise ValueError('Provide at least one displayed evidence ID')
        return evidence.read(ids, refresh=True)

    return {'read_evidence': read_evidence}


def _validate_writer_binding(result, payload):
    if result.failure is not None:
        return
    group = payload['group']
    from cortex.capabilities.graph_merge.integration.node_revision import partition_groups
    partition_groups(group, result.integrate_groups, result.keep_separate_ids)
    members = set(group['member_ids'])
    edge_rows = {row['id']: row for row in _relation_rows(payload.get('relations', []))}
    removed = set()
    for removal in result.relation_removals:
        edge = edge_rows.get(removal.edge_id)
        if (removal.edge_id in removed or edge is None
                or not {edge.get('from_node_id'), edge.get('to_node_id')} <= members):
            raise PatchContractError('node_relation_removal_outside_group', owner_id=group['owner_id'],
                path='relation_removals', edge_id=removal.edge_id,
                required_action='Use only distinct displayed original edges whose two endpoints are group members.')
        if not removal.reason.strip():
            raise ValueError('Relation removal reasons must be nonempty')
        removed.add(removal.edge_id)
    for index, integration in enumerate(result.integrate_groups):
        kind = group['node_type']
        allowed_nodes = set(group['member_ids']) | {row['id'] for row in payload.get('endpoint_directory', [])}
        contents = ([(f'integrate_groups[{index}].content', integration.content)] if integration.content is not None
                    else [(f'integrate_groups[{index}].alternatives[{branch_index}].content', row.content)
                          for branch_index, row in enumerate(integration.alternatives)])
        for path, content in contents:
            validate_semantic_properties(kind, content.properties)
            unknown = property_reference_ids(content.properties) - allowed_nodes
            if unknown:
                raise PatchContractError('node_reference_outside_local_input', owner_id=group['owner_id'],
                    path=path + '.properties', extra=sorted(unknown), allowed_node_ids=sorted(allowed_nodes),
                    required_action='Use only node IDs displayed in this local group or endpoint directory. Report a concrete missing premise instead of guessing a global ID.')

        rows = payload.get('source_unit_manifest', [])
        if isinstance(rows, dict):
            rows = [rows]
        expected = {unit['unit_id']: unit for row in rows
                    if row.get('owner_id') == group['owner_id']
                    for unit in row.get('source_units', [])
                    if unit.get('node_id') in integration.member_ids}
        counts = Counter(route.source_unit_id for route in integration.source_routes)
        if not expected or counts != Counter({ident: 1 for ident in expected}):
            raise PatchContractError('source_routes_mismatch', owner_id=group['owner_id'],
                path=f'integrate_groups[{index}].source_routes',
                missing=sorted(expected.keys() - counts.keys()), extra=sorted(counts.keys() - expected.keys()),
                duplicates=sorted(key for key, count in counts.items() if count > 1),
                required_source_units=list(expected.values()),
                required_action='Route each displayed real source unit exactly once; remove extra and duplicate routes.')
        alternatives = {row.alternative_id for row in integration.alternatives}
        for route in integration.source_routes:
            destination = route.destination
            if destination.kind == 'discard' and expected[route.source_unit_id]['kind'] != 'alternative':
                raise ValueError('Only an old alternative source unit may be discarded')
            if destination.kind == 'result' and alternatives:
                raise ValueError('Conflict results cannot use result source routes')
            if destination.kind in {'common', 'alternatives'} and not alternatives:
                raise ValueError('Normal content uses result source routes')
            if set(destination.alternative_ids) - alternatives:
                raise ValueError('Source route names an unknown output alternative')
        known_evidence = local_evidence_ids(payload)
        cited = set(integration.common_evidence_ids)
        cited.update(ident for branch in integration.alternatives for ident in branch.evidence_ids)
        if cited - known_evidence:
            raise ValueError('Cite only evidence IDs displayed in this local group')


def _validate_review_binding(result, payload):
    group = payload['group']
    before_nodes = set(group['member_ids'])
    before_nodes.update(row['id'] for row in payload.get('endpoint_directory', []) if 'id' in row)
    candidate = payload.get('candidate', {})
    candidate_nodes = set(before_nodes - set(group['member_ids']))
    candidate_nodes.update(row['id'] for row in candidate.get('endpoint_directory', []) if 'id' in row)
    candidate_nodes.update(row['id'] for row in candidate.get('nodes', []) if 'id' in row)
    before_edges = local_original_edge_ids(payload)
    candidate_edges = {row['id'] for row in _relation_rows(candidate.get('relations', [])) if 'id' in row}
    contracts = {row.get('id', row.get('edge_type')) for row in
                 payload.get('relation_contracts', payload.get('relation_types', []))}
    for issue in result.issues:
        for ref in (*issue.refs, *issue.basis):
            allowed = (before_nodes if ref.view == 'before' else candidate_nodes) if ref.kind == 'node' else (
                before_edges if ref.view == 'before' else candidate_edges) if ref.kind == 'edge' else contracts
            if ref.id not in allowed:
                raise ValueError('Node assessment references must use displayed local object IDs; describe missing premises in text')


def normalize_node_result(role, value, *, payload=None, allowed_edge_ids=None, base=None):
    """Return legacy result and a sidecar; neither is committed by this function.

    Packet binding is an additional local permission check. The real compiler and
    validate_review must still validate graph structure, source units and paths.
    """
    role = node_role(role)
    local = node_output_model(role).model_validate(value)
    allowed = set(allowed_edge_ids if allowed_edge_ids is not None else
                  local_original_edge_ids(payload or {}))
    if any(set(item.edge_ids) - allowed for item in local.relation_followups):
        raise ValueError('Followups must reference stable original edges displayed in this local group')
    if payload is not None:
        (_validate_writer_binding if role == 'writer' else _validate_review_binding)(local, payload)
    legacy = (MergePatch if role == 'writer' else Review).model_validate(
        local.model_dump(mode='json', exclude={'relation_followups'}, exclude_unset=True))
    sidecar = tuple(row.model_dump(mode='json') for row in local.relation_followups)
    return legacy, sidecar
