"""Executable proposal shapes for review only; not imported by production."""
from __future__ import annotations

from typing import Annotated, Literal
from copy import deepcopy

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic.json_schema import SkipJsonSchema
from cortex.schema import NodeContent, ConflictPoint
from cortex.capabilities.graph_merge.graph.edits import EvidenceAlternative, BusinessContract
from cortex.capabilities.graph_merge.graph.source_units import SourceRoute
from cortex.capabilities.graph_merge.consolidation.review import ReviewReference, AbsenceClaim
from cortex.schema.semantic_validation import validate_semantic_properties

Text = Annotated[str, Field(min_length=1, pattern=r'\S')]
NodeType = Literal['concept', 'metric', 'event', 'generation_rule']


class Model(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)


class ReadNodes(Model):
    ids: tuple[Text, ...] = Field(min_length=1)
    view: Literal['before', 'candidate', 'source'] = 'before'
    fields: tuple[Text, ...] = ('summary',)
    draft_version: int | None = Field(default=None, ge=0)
    alternative_ids: tuple[Text, ...] = ()
    source_snapshot_id: Text | None = None

    @model_validator(mode='after')
    def binding(self):
        if self.view == 'candidate' and self.draft_version is None:
            raise ValueError('Candidate reads require an exact compiled draft_version')
        if self.alternative_ids and 'alternatives' not in self.fields:
            raise ValueError('Alternative selection requires the alternatives selector')
        if (self.view == 'source') != (self.source_snapshot_id is not None):
            raise ValueError('Source reads identify one controller-authorized archive snapshot')
        if self.view == 'source' and ('source_units' in self.fields or self.draft_version is not None):
            raise ValueError('Source archives are readonly history, not current routing/candidate snapshots')
        return self


class ReadEvidence(Model):
    ids: tuple[Text, ...] = Field(min_length=1)


class RequestContext(Model):
    kind: Literal['relations', 'catalog', 'write_contract']
    reason: Text
    anchor_ids: tuple[Text, ...] = ()
    view: Literal['before', 'candidate'] = 'before'
    draft_version: int | None = Field(default=None, ge=0)
    direction: Literal['incoming', 'outgoing', 'both'] = 'both'
    edge_types: tuple[Text, ...] = ()
    cursor: str | None = None
    table: str | None = None
    columns: tuple[Text, ...] = ()
    node_type: NodeType | None = None
    operation_kind: Literal['edge_update', 'edge_create', 'edge_merge', 'retire', 'node_update', 'node_create', 'node_rewrite', 'relation_declare'] | None = None

    @model_validator(mode='after')
    def request_scope(self):
        if self.view == 'candidate' and self.draft_version is None:
            raise ValueError('Candidate context requires compiled draft_version')
        if self.kind == 'relations' and not self.anchor_ids:
            raise ValueError('Relation context needs displayed anchors')
        if self.kind == 'catalog' and not self.table:
            raise ValueError('Catalog context needs an exact table locator')
        if self.kind == 'write_contract' and (not self.node_type or not self.operation_kind):
            raise ValueError('Write contract needs node_type and operation_kind')
        if self.kind == 'write_contract' and self.operation_kind in {'node_update', 'node_rewrite', 'retire'} and not self.anchor_ids:
            raise ValueError('Existing-node writes need a concrete owner anchor')
        if self.kind != 'relations' and (self.edge_types or self.cursor or self.direction != 'both'):
            raise ValueError('Relation selectors only apply to relations requests')
        if self.kind != 'catalog' and (self.table is not None or self.columns):
            raise ValueError('Catalog selectors only apply to catalog requests')
        if self.kind != 'write_contract' and (self.node_type is not None or self.operation_kind is not None):
            raise ValueError('Write-contract selectors only apply to write_contract requests')
        return self


class NodeUpdateValue(Model):
    id: Text
    explanation: Text
    name: str | SkipJsonSchema[None] = None
    description: str | None = None
    aliases: tuple[str, ...] | SkipJsonSchema[None] = None
    tags: tuple[str, ...] | SkipJsonSchema[None] = None
    properties: dict = Field(default_factory=dict)
    add_evidence: tuple[Text, ...] = ()

    @model_validator(mode='before')
    @classmethod
    def no_null_replacement(cls, value):
        if isinstance(value, dict) and any(key in value and value[key] is None for key in ('name', 'aliases', 'tags')):
            raise ValueError('Optional fields are omitted, not null; description alone permits null')
        return value


class EdgeUpdateValue(Model):
    id: Text
    explanation: Text
    edge_type: str | SkipJsonSchema[None] = None
    from_node_id: str | SkipJsonSchema[None] = None
    to_node_id: str | SkipJsonSchema[None] = None
    properties: dict = Field(default_factory=dict)
    add_evidence: tuple[Text, ...] = ()

    @model_validator(mode='before')
    @classmethod
    def no_null_fields(cls, value):
        if isinstance(value, dict) and any(key in value and value[key] is None for key in ('edge_type', 'from_node_id', 'to_node_id')):
            raise ValueError('Optional edge fields are omitted, not null')
        return value


class CreateNodeValue(Model):
    id: Text
    node_type: NodeType
    content: NodeContent | None = None
    alternatives: tuple[EvidenceAlternative, ...] = ()
    conflict_points: tuple[ConflictPoint, ...] = ()
    evidence_ids: tuple[Text, ...] = ()
    common_evidence_ids: tuple[Text, ...] = ()
    explanation: Text

    @model_validator(mode='after')
    def complete_node(self):
        if (self.content is None) == (not self.alternatives):
            raise ValueError('Create normal content or complete conflict alternatives, exclusively')
        if self.content is not None:
            if not self.evidence_ids or self.conflict_points or self.common_evidence_ids:
                raise ValueError('Normal creation needs evidence_ids and no conflict-only fields')
            validate_semantic_properties(self.node_type, self.content.properties)
        else:
            if len(self.alternatives) < 2 or not self.conflict_points or self.evidence_ids:
                raise ValueError('Conflict creation needs >=2 branches, conflict_points and branch attribution')
            ids = [item.alternative_id for item in self.alternatives]
            if any(not key.strip() for key in ids) or len(ids) != len(set(ids)) or any(not item.evidence_ids for item in self.alternatives):
                raise ValueError('New complete branches need distinct IDs and their own supporting evidence')
            for item in self.alternatives:
                validate_semantic_properties(self.node_type, item.content.properties)
            validate_points(self.conflict_points, ids)
        return self


class CreateEdgeValue(Model):
    id: Text
    edge_type: Text
    from_node_id: Text
    to_node_id: Text
    properties: dict
    evidence_ids: tuple[Text, ...] = Field(min_length=1)
    explanation: Text

    @model_validator(mode='after')
    def edge_description(self):
        if not isinstance(self.properties.get('description'), str) or not self.properties['description'].strip():
            raise ValueError('New relations need complete properties.description')
        if set(self.properties) & {'condition', 'role', 'qualifier', 'qualifiers', 'sources'}:
            raise ValueError('Do not invent edge properties; use declared description/scope')
        return self


class MergeEdgesValue(Model):
    source_ids: tuple[Text, ...] = Field(min_length=1)
    survivor_id: Text
    explanation: Text

    @model_validator(mode='after')
    def distinct_edges(self):
        if len(set(self.source_ids)) != len(self.source_ids) or self.survivor_id in self.source_ids:
            raise ValueError('Absorbed edge IDs are distinct and exclude the survivor')
        return self


class Element(Model):
    kind: Literal['node', 'edge']
    id: Text

class SuccessorReference(Element):
    alternative_id: str | None = None

    @model_validator(mode='after')
    def branch_owner(self):
        if self.alternative_id is not None and (self.kind != 'node' or not self.alternative_id.strip()):
            raise ValueError('Only a node successor may identify an actual nonempty branch')
        return self

class RetiredNodeUnitRoute(Model):
    source_node_id: Text
    source_unit_id: Text
    successors: tuple[SuccessorReference, ...] = Field(min_length=1)
    explanation: Text


class RetireValue(Model):
    before: tuple[Element, ...] = Field(min_length=1)
    after: tuple[Element, ...] = ()
    explanation: Text
    node_unit_routes: tuple[RetiredNodeUnitRoute, ...] = ()

    @model_validator(mode='after')
    def node_preservation(self):
        nodes = {item.id for item in self.before if item.kind == 'node'}
        if nodes:
            if not self.after or not self.node_unit_routes:
                raise ValueError('Retiring a node needs actual successors and complete source-unit routes')
            if {row.source_node_id for row in self.node_unit_routes} != nodes:
                raise ValueError('Node retirement routes must cover every and only retired source node')
            keys = [(row.source_node_id, row.source_unit_id) for row in self.node_unit_routes]
            if len(set(keys)) != len(keys):
                raise ValueError('Node retirement source units are routed once')
            actual = {(item.kind, item.id) for item in self.after}
            if any((item.kind, item.id) not in actual for row in self.node_unit_routes for item in row.successors):
                raise ValueError('Source units must locate actual declared successors')
        elif self.node_unit_routes:
            raise ValueError('Edge-only retirements do not invent node source-unit routes')
        return self

class NodeRewriteValue(Model):
    node_id: Text
    content: NodeContent | None = None
    alternatives: tuple[EvidenceAlternative, ...] = ()
    conflict_points: tuple[ConflictPoint, ...] = ()
    common_evidence_ids: tuple[Text, ...] = ()
    evidence_ids: tuple[Text, ...] = ()
    source_routes: tuple[SourceRoute, ...] = Field(min_length=1)
    explanation: Text

    @model_validator(mode='after')
    def complete_owner_result(self):
        if (self.content is None) == (not self.alternatives):
            raise ValueError('Rewrite one owner as complete normal content OR complete alternatives')
        ids = [row.alternative_id for row in self.alternatives]
        if self.alternatives:
            if len(ids) < 2 or any(not key.strip() for key in ids) or len(set(ids)) != len(ids) or not self.conflict_points:
                raise ValueError('Conflict rewrites need >=2 unique complete branches and conflict points')
            validate_points(self.conflict_points, ids)
            if self.evidence_ids:
                raise ValueError('Conflict rewrites attribute new support to branches/common, not normal evidence_ids')
        elif self.conflict_points or self.common_evidence_ids:
            raise ValueError('Normal rewrites have no conflict-only fields')
        units = [row.source_unit_id for row in self.source_routes]
        if len(set(units)) != len(units):
            raise ValueError('Every source unit is routed at most once; binding checks exact coverage')
        for route in self.source_routes:
            if self.alternatives and route.destination.kind == 'result':
                raise ValueError('Conflict results cannot use result routes')
            if not self.alternatives and route.destination.kind in {'common', 'alternatives'}:
                raise ValueError('Normal results use result/discard routes')
            if set(route.destination.alternative_ids) - set(ids):
                raise ValueError('Source routes must name actual output branches')
        return self


def validate_points(points, branch_ids):
    point_ids = [point.point_id for point in points]
    if len(point_ids) != len(set(point_ids)):
        raise ValueError('Conflict point IDs must be unique')
    for point in points:
        positions = [set(choice.alternative_ids) for choice in point.choices]
        if any(not values or values - set(branch_ids) for values in positions):
            raise ValueError('Conflict points reference actual output alternatives')
        if len(set().union(*positions)) < 2 or len({tuple(sorted(values)) for values in positions}) < 2:
            raise ValueError('Each conflict point must distinguish at least two branches')


class EdgeUpdate(Model):
    change_id: Text
    kind: Literal['edge_update']
    value: EdgeUpdateValue

class EdgeCreate(Model):
    change_id: Text
    kind: Literal['edge_create']
    value: CreateEdgeValue

class EdgeMerge(Model):
    change_id: Text
    kind: Literal['edge_merge']
    value: MergeEdgesValue

class Retire(Model):
    change_id: Text
    kind: Literal['retire']
    value: RetireValue

class NodeUpdate(Model):
    change_id: Text
    kind: Literal['node_update']
    value: NodeUpdateValue

class NodeCreate(Model):
    change_id: Text
    kind: Literal['node_create']
    value: CreateNodeValue

class NodeRewrite(Model):
    change_id: Text
    kind: Literal['node_rewrite']
    value: NodeRewriteValue

class RelationDeclare(Model):
    change_id: Text
    kind: Literal['relation_declare']
    value: BusinessContract

Change = Annotated[EdgeUpdate | EdgeCreate | EdgeMerge | Retire | NodeUpdate | NodeCreate | NodeRewrite | RelationDeclare,
                   Field(discriminator='kind')]


class SignalDecision(Model):
    signal_id: Text
    status: Literal['pending', 'keep', 'repair', 'needs_context', 'needs_identity']
    reason: Text
    change_ids: tuple[Text, ...] = ()

    @model_validator(mode='after')
    def decision_changes(self):
        if self.status == 'repair' and not self.change_ids:
            raise ValueError('Repair decisions must name their current draft changes')
        if self.status == 'keep' and self.change_ids:
            raise ValueError('Keep decisions do not claim a repair change')
        if len(set(self.change_ids)) != len(self.change_ids):
            raise ValueError('Decision change IDs must be distinct')
        return self


class PutChange(Model):
    op: Literal['put_change']
    change: Change

class RemoveChange(Model):
    op: Literal['remove_change']
    change_id: Text

class PutDecision(Model):
    op: Literal['put_decision']
    decision: SignalDecision

Edit = Annotated[PutChange | RemoveChange | PutDecision, Field(discriminator='op')]


class EditDraft(Model):
    expected_version: int = Field(ge=0)
    operations: tuple[Edit, ...] = Field(min_length=1)

class SubmitDraft(Model):
    expected_version: int = Field(ge=0)

class DraftDocument(Model):
    task_id: Text
    before_id: Text
    version: int = Field(ge=0)
    changes: tuple[Change, ...] = ()
    decisions: tuple[SignalDecision, ...] = ()

    @model_validator(mode='after')
    def unique_handles(self):
        for rows, key in ((self.changes, 'change_id'), (self.decisions, 'signal_id')):
            ids = [getattr(row, key) for row in rows]
            if len(set(ids)) != len(ids):
                raise ValueError(f'Duplicate {key}')
        return self


class ProposalReference(ReviewReference):
    view: Literal['before', 'candidate', 'source'] = 'candidate'
    source_snapshot_id: Text | None = None

    @model_validator(mode='after')
    def archive_binding(self):
        if (self.view == 'source') != (self.source_snapshot_id is not None):
            raise ValueError('Historical references must identify their precise authorized snapshot')
        return self

class ProposalAbsenceClaim(AbsenceClaim):
    view: Literal['before', 'candidate'] = 'candidate'


class ReviewIssue(Model):
    issue_id: Text
    kind: Literal['defect', 'missing_context', 'identity_counterexample'] = 'defect'
    signal_ids: tuple[Text, ...] = ()
    refs: tuple[ProposalReference, ...] = Field(min_length=1)
    basis: tuple[ProposalReference, ...] = ()
    problem: Text
    required_outcome: Text
    claim: ProposalAbsenceClaim | None = None

class SignalReview(Model):
    signal_id: Text
    status: Literal['satisfied', 'needs_revision', 'needs_context', 'needs_identity']
    explanation: Text
    issue_ids: tuple[Text, ...] = ()

class PriorIssueReview(Model):
    issue_id: Text
    status: Literal['resolved', 'still_open', 'needs_context', 'needs_identity']
    explanation: Text
    issue_ids: tuple[Text, ...] = ()

    @model_validator(mode='after')
    def resolution_links(self):
        if (self.status == 'resolved') != (not self.issue_ids):
            raise ValueError('Resolved old issues have no current issue links; open ones name current issues')
        return self

class AssessmentReview(Model):
    expected_version: int = Field(ge=0)
    verdict: Literal['accept', 'revise', 'needs_context', 'revisit_identity']
    signal_reviews: tuple[SignalReview, ...] = Field(min_length=1)
    prior_issue_reviews: tuple[PriorIssueReview, ...] = ()
    issues: tuple[ReviewIssue, ...] = ()

    @model_validator(mode='after')
    def coherent_review(self):
        ids = [row.issue_id for row in self.issues]
        signals = [row.signal_id for row in self.signal_reviews]
        if len(set(ids)) != len(ids) or len(set(signals)) != len(signals):
            raise ValueError('Review IDs must be distinct')
        if any(set(row.issue_ids) - set(ids) for row in self.signal_reviews):
            raise ValueError('Signal reviews reference actual issue IDs')
        prior_ids = [row.issue_id for row in self.prior_issue_reviews]
        if len(prior_ids) != len(set(prior_ids)) or any(set(row.issue_ids) - set(ids) for row in self.prior_issue_reviews):
            raise ValueError('Old issue reviews must be unique and link current issue IDs')
        if self.verdict == 'accept':
            if (self.issues or any(row.status != 'satisfied' or row.issue_ids for row in self.signal_reviews)
                    or any(row.status != 'resolved' for row in self.prior_issue_reviews)):
                raise ValueError('Accept means every signal satisfied and no issues')
        elif not self.issues:
            raise ValueError('Blocking reviews require concrete issues')
        def severity(kinds):
            return ('identity_counterexample' if 'identity_counterexample' in kinds else
                    'missing_context' if 'missing_context' in kinds else 'defect' if kinds else None)
        actual = severity({row.kind for row in self.issues})
        required = {None: 'accept', 'defect': 'revise', 'missing_context': 'needs_context', 'identity_counterexample': 'revisit_identity'}[actual]
        if self.verdict != required:
            raise ValueError('Verdict must describe all current issues, including new regressions')
        if self.verdict in {'revise', 'revisit_identity'} and any(not row.basis for row in self.issues):
            raise ValueError('Blocking semantic judgments need an explicit basis')
        for row in self.signal_reviews:
            if row.status != 'satisfied' and not row.issue_ids:
                raise ValueError('Unresolved signal reviews must link concrete issues')
            linked = [item for item in self.issues if item.issue_id in row.issue_ids]
            reverse = {item.issue_id for item in self.issues if row.signal_id in item.signal_ids}
            if set(row.issue_ids) != reverse:
                raise ValueError('Signal/issue links must agree in both directions')
            expected = {None:'satisfied', 'defect':'needs_revision', 'missing_context':'needs_context', 'identity_counterexample':'needs_identity'}[severity({item.kind for item in linked})]
            if row.status != expected:
                raise ValueError('Signal status must agree with its current linked issues')
        for row in self.prior_issue_reviews:
            if row.status == 'resolved':
                continue
            linked = [item for item in self.issues if item.issue_id in row.issue_ids]
            expected = {'defect':'still_open', 'missing_context':'needs_context', 'identity_counterexample':'needs_identity'}[severity({item.kind for item in linked})]
            if row.status != expected:
                raise ValueError('Prior-issue status must agree with current linked issues')
        return self


TOOLS = {'read_nodes': ReadNodes, 'read_evidence': ReadEvidence, 'request_context': RequestContext,
         'edit_draft': EditDraft, 'submit_draft': SubmitDraft, 'Review': AssessmentReview}


def public_schema(model):
    """No null default is presented as a recommendation to fill optional fields."""
    body = deepcopy(model.model_json_schema())
    def visit(value):
        if isinstance(value, dict):
            if value.get('default', object()) is None:
                value.pop('default', None)
            for item in value.values():
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)
    visit(body)
    return body


def bind_review(value, *, draft_version, signal_ids, visible_node_ids, visible_edge_ids, contract_ids=(), pending_issue_ids=()):
    result = AssessmentReview.model_validate(value)
    if result.expected_version != draft_version:
        raise ValueError('Review addresses a stale draft version')
    if {row.signal_id for row in result.signal_reviews} != set(signal_ids):
        raise ValueError('Review must cover every current trigger exactly once')
    if {row.issue_id for row in result.prior_issue_reviews} != set(pending_issue_ids):
        raise ValueError('Review must explicitly reassess every pending old issue')
    for issue in result.issues:
        if set(issue.signal_ids) - set(signal_ids):
            raise ValueError('Unknown issue signal ID')
        for ref in (*issue.refs, *issue.basis):
            if ref.view == 'input':
                raise ValueError('Use the explicit local before/candidate views')
            inventory = visible_node_ids if ref.kind == 'node' else visible_edge_ids if ref.kind == 'edge' else set(contract_ids)
            if ref.view == 'source':
                allowed = inventory.get('source', {}).get(ref.source_snapshot_id, set()) if isinstance(inventory, dict) else set()
            else:
                allowed = inventory.get(ref.view, set()) if isinstance(inventory, dict) else inventory
            if ref.id not in allowed:
                raise ValueError('Review references an unprovided object')
        if issue.claim is not None:
            inventory = visible_node_ids
            allowed = inventory.get(issue.claim.view, set()) if isinstance(inventory, dict) else inventory
            endpoints = {issue.claim.to_node_id} | ({issue.claim.from_node_id} if issue.claim.from_node_id else set())
            if endpoints - allowed:
                raise ValueError('Absence claims cannot probe unauthorized endpoints')
    return result


def apply_edit_shape(document, payload, *, signal_ids, current_stage='editing'):
    """Review-only protocol simulation: no graph, I/O, permissions or LLM."""
    draft, edit = DraftDocument.model_validate(document), EditDraft.model_validate(payload)
    if current_stage not in {'editing', 'revising'}:
        raise ValueError('STAGE_MISMATCH: submitted head is locked against late edits')
    if draft.version != edit.expected_version:
        raise ValueError('VERSION_MISMATCH: edit was not applied')
    changes = {item.change_id: item.model_dump(mode='json', exclude_unset=True) for item in draft.changes}
    decisions = {item.signal_id: item.model_dump(mode='json', exclude_unset=True) for item in draft.decisions}
    touched = set()
    for item in edit.operations:
        key = ('decision', item.decision.signal_id) if isinstance(item, PutDecision) else ('change', item.change.change_id if isinstance(item, PutChange) else item.change_id)
        if key in touched:
            raise ValueError('One atomic edit may not target the same change/decision twice')
        touched.add(key)
        if isinstance(item, PutChange):
            changes[item.change.change_id] = item.change.model_dump(mode='json', exclude_unset=True)
        elif isinstance(item, RemoveChange):
            if item.change_id not in changes:
                raise ValueError('Unknown draft change ID; no part of this edit was applied')
            del changes[item.change_id]
        else:
            if item.decision.signal_id not in signal_ids:
                raise ValueError('Unknown task Signal ID; no part of this edit was applied')
            decisions[item.decision.signal_id] = item.decision.model_dump(mode='json', exclude_unset=True)
    return DraftDocument(task_id=draft.task_id, before_id=draft.before_id, version=draft.version + 1,
        changes=list(changes.values()), decisions=list(decisions.values()))


def submission_readiness(document, payload, *, signal_ids):
    draft, request = DraftDocument.model_validate(document), SubmitDraft.model_validate(payload)
    if draft.version != request.expected_version:
        raise ValueError('VERSION_MISMATCH: no candidate was submitted')
    decisions = {row.signal_id: row for row in draft.decisions}
    if decisions.keys() != set(signal_ids) or any(row.status == 'pending' for row in decisions.values()):
        raise ValueError('Every current trigger needs a non-pending decision')
    if any(row.status in {'needs_context', 'needs_identity'} for row in decisions.values()):
        return 'deferred_not_accepted'
    named = {key for row in decisions.values() for key in row.change_ids}
    actual = {row.change_id for row in draft.changes}
    if named != actual:
        raise ValueError('Each draft change must be linked to a repair decision; unknown/missing change IDs')
    validate_combinations(draft.changes)
    return 'ready_for_real_compile_not_yet_accepted'


def validate_combinations(changes):
    writes, removed, absorbed, merged = {}, set(), set(), set()
    created_edges = {row.value.id for row in changes if row.kind == 'edge_create'}
    before_refs = set()
    for row in changes:
        if row.kind in {'node_update', 'node_create', 'node_rewrite', 'edge_update', 'edge_create'}:
            category = 'node' if row.kind.startswith('node_') else 'edge'
            ident = row.value.node_id if row.kind == 'node_rewrite' else row.value.id
            key = (category, ident)
            if key in writes:
                raise ValueError('One graph object has one content-writing draft change')
            writes[key] = row.change_id
        elif row.kind == 'edge_merge':
            group = set(row.value.source_ids) | {row.value.survivor_id}
            if group & merged or group & created_edges:
                raise ValueError('Edge merges use disjoint existing edges, without merge chains')
            merged |= group
            absorbed |= {('edge', key) for key in row.value.source_ids}
        elif row.kind == 'retire':
            before = {(item.kind, item.id) for item in row.value.before}
            after = {(item.kind, item.id) for item in row.value.after}
            if before & before_refs or not before - after:
                raise ValueError('Retirement requires a distinct actual removal with one disposition')
            before_refs |= before
            removed |= before - after
    if (removed | absorbed) & writes.keys() or removed & absorbed:
        raise ValueError('Removed/absorbed objects are not also updated or retired twice')
    if {('edge', ident) for ident in merged} & removed:
        raise ValueError('Edge merge participants cannot also be retired')


def bind_node_rewrite(value, *, manifest, node_type, allowed_evidence_ids):
    """Check proposed single-owner routing shape, not truth or real compilation."""
    result = NodeRewriteValue.model_validate(value)
    if manifest['owner_id'] != result.node_id or set(manifest.get('member_ids', [result.node_id])) != {result.node_id}:
        raise ValueError('Node rewrite is one existing identity, not an integration group')
    units = {row['unit_id']: row for row in manifest['source_units']}
    if {row.source_unit_id for row in result.source_routes} != units.keys():
        raise ValueError('Exact source-unit coverage is required')
    for route in result.source_routes:
        if route.destination.kind == 'discard' and units[route.source_unit_id]['kind'] != 'alternative':
            raise ValueError('Normal/common input units cannot be discarded')
    known = set(allowed_evidence_ids)
    if set(result.common_evidence_ids) - known:
        raise ValueError('Unknown common evidence ID')
    if set(result.evidence_ids) - known:
        raise ValueError('Unknown normal-result evidence ID')
    contents = [result.content] if result.content is not None else [row.content for row in result.alternatives]
    for content in contents:
        validate_semantic_properties(node_type, content.properties)
    for branch in result.alternatives:
        if set(branch.evidence_ids) - known:
            raise ValueError('Unknown branch evidence ID')
        supported = branch.evidence_ids or any(route.destination.kind == 'common' or
            branch.alternative_id in route.destination.alternative_ids for route in result.source_routes)
        if not supported:
            raise ValueError('Each output branch needs direct evidence or an actual supporting source route')
    return result


def bind_node_retirement(value, *, manifests, available_successors):
    """Shape/reference coverage only; semantic preservation remains unproved."""
    result = RetireValue.model_validate(value)
    for node in (row for row in result.before if row.kind == 'node'):
        manifest = manifests.get(node.id)
        if manifest is None:
            raise ValueError('Node retirement requires its actual source-unit manifest')
        expected = {row['unit_id'] for row in manifest['source_units']}
        actual = {row.source_unit_id for row in result.node_unit_routes if row.source_node_id == node.id}
        if actual != expected:
            raise ValueError('Retired node source units must have exact preservation coverage')
    for route in result.node_unit_routes:
        for ref in route.successors:
            key = (ref.kind, ref.id)
            if key not in available_successors:
                raise ValueError('A declared successor must actually exist in the candidate')
            if ref.alternative_id is not None and ref.alternative_id not in available_successors[key]:
                raise ValueError('A successor branch must actually exist')
    return result
