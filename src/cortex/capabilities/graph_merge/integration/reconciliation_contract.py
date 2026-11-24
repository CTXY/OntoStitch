"""Local graph edits and a two-part independent assessment result.

Only business edits and feedback are public. Versions, acceptance and scheduling
belong to the controller; there is no model-authored decision state machine.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field, model_validator
from pydantic.json_schema import SkipJsonSchema

from cortex.schema import NodeContent
from cortex.schema.semantic_validation import validate_semantic_properties
from cortex.capabilities.graph_merge.integration.relation_edits import BusinessContract

Text = Annotated[str, Field(min_length=1, pattern=r'\S')]
NodeType = Literal['concept', 'metric', 'event', 'generation_rule']


class Model(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)

    @classmethod
    def model_json_schema(cls, *args, **kwargs):
        schema = super().model_json_schema(*args, **kwargs)
        def clean(value):
            if isinstance(value, dict):
                if value.get('default', ...) is None:
                    value.pop('default', None)
                for child in value.values(): clean(child)
            elif isinstance(value, list):
                for child in value: clean(child)
        clean(schema)
        return schema


class ReadNodes(Model):
    ids: tuple[Text, ...] = Field(min_length=1)


class ReadRelations(Model):
    node_ids: tuple[Text, ...] = Field(min_length=1)


class ReadEvidence(Model):
    ids: tuple[Text, ...] = Field(min_length=1)


class ReadPredicates(Model):
    from_node_type: NodeType = Field(description='Business source type needed for a justified local relation; this reads schema only.')
    to_node_type: NodeType | Literal['table','column','dimension_value'] = Field(description='Actual or proposed target type. No node IDs or graph traversal.')


class ObjectReference(Model):
    view: Literal['before', 'candidate']
    kind: Literal['node', 'edge', 'contract']
    id: Text = Field(description='Actual displayed object ID in the current ontology graph.')
    field: str | SkipJsonSchema[None] = Field(default=None,
        description='Optional exact public field path, e.g. evidence_ids or properties.definition. No private storage paths or array indices.')


class BeforeReference(ObjectReference):
    view: Literal['before'] = 'before'


class MissingRelationReference(Model):
    kind: Literal['missing_relation']
    view: Literal['before', 'candidate'] = 'candidate'
    from_node_id: Text = Field(description='Actual existing displayed source node ID in this view; never a hypothetical new node.')
    edge_type: Text
    to_node_id: Text = Field(description='Actual existing displayed target node ID in this view; cite an existing anchor to report a missing node.')
    alternative_id: str | SkipJsonSchema[None] = Field(default=None,
        description='Optional source alternative only, never a target version selector.')


Reference = Annotated[ObjectReference | MissingRelationReference, Field(discriminator='kind')]


class NodeFields(Model):
    name: str | SkipJsonSchema[None] = None
    description: str | None = None
    aliases: tuple[str, ...] | SkipJsonSchema[None] = None
    tags: tuple[str, ...] | SkipJsonSchema[None] = None
    properties: dict = Field(default_factory=dict)

    @model_validator(mode='before')
    @classmethod
    def nullable_description_only(cls, value):
        if isinstance(value, dict) and any(value.get(k, ...) is None for k in ('name', 'aliases', 'tags')):
            raise ValueError('Only description allows null; omit other fields to preserve them')
        return value


class EdgeFields(Model):
    edge_type: str | SkipJsonSchema[None] = None
    from_node_id: str | SkipJsonSchema[None] = None
    to_node_id: str | SkipJsonSchema[None] = None
    properties: dict = Field(default_factory=dict)

    @model_validator(mode='before')
    @classmethod
    def no_null_endpoints(cls, value):
        if isinstance(value, dict) and any(value.get(k, ...) is None for k in ('edge_type', 'from_node_id', 'to_node_id')):
            raise ValueError('Omit unchanged edge fields; endpoints and predicate cannot be null')
        return value


class ChangeBase(Model):
    change_id: Text
    reason: Text


class NodeCreate(ChangeBase):
    kind: Literal['node_create']
    node_id: Text
    node_type: NodeType
    content: NodeContent
    evidence_ids: tuple[Text, ...] = Field(min_length=1)

    @model_validator(mode='after')
    def complete_content(self):
        validate_semantic_properties(self.node_type, self.content.properties)
        return self


class NodeUpdate(ChangeBase):
    kind: Literal['node_update']
    node_id: Text
    set: NodeFields
    unset: tuple[Text, ...] = Field(default=(), description='Optional properties.<key> fields to remove explicitly. Never IDs, types or sources.')
    evidence_ids: tuple[Text, ...] = Field(default=(), description='Additional supporting evidence; original sources are retained.')


class EdgeCreate(ChangeBase):
    kind: Literal['edge_create']
    edge_id: Text
    edge_type: Text
    from_node_id: Text
    to_node_id: Text
    properties: dict
    evidence_ids: tuple[Text, ...] = Field(min_length=1)

    @model_validator(mode='after')
    def description_required(self):
        if not isinstance(self.properties.get('description'), str) or not self.properties['description'].strip():
            raise ValueError('New relations require a concrete properties.description')
        return self


class EdgeUpdate(ChangeBase):
    kind: Literal['edge_update']
    edge_id: Text
    set: EdgeFields
    unset: tuple[Text, ...] = Field(default=(), description='Optional properties.<key> fields to remove explicitly.')
    evidence_ids: tuple[Text, ...] = ()


class EdgeReplace(ChangeBase):
    kind: Literal['edge_replace']
    edge_id: Text
    replacement_edge_id: Text = Field(description='Actual surviving edge carrying the retained knowledge. Controller unions exact sources, never guesses a successor.')

    @model_validator(mode='after')
    def distinct_edges(self):
        if self.edge_id == self.replacement_edge_id:
            raise ValueError('A replacement must identify a distinct actual surviving edge')
        return self


class EdgeRemove(ChangeBase):
    kind: Literal['edge_remove']
    edge_id: Text
    refs: tuple[BeforeReference, ...] = Field(min_length=1,
        description='Visible before facts justifying removal of an unsupported/false assertion. A stale edge with retained knowledge needs edge_replace instead.')


class RelationDeclare(ChangeBase):
    kind: Literal['relation_declare']
    contract: BusinessContract


Change = Annotated[NodeCreate | NodeUpdate | EdgeCreate | EdgeUpdate | EdgeReplace | EdgeRemove | RelationDeclare,
                   Field(discriminator='kind')]


class EditDraft(Model):
    put_changes: tuple[Change, ...] = ()
    remove_changes: tuple[Text, ...] = ()

    @model_validator(mode='after')
    def one_edit_per_handle(self):
        ids = [row.change_id for row in self.put_changes]
        if len(ids) != len(set(ids)) or len(self.remove_changes) != len(set(self.remove_changes)) or set(ids) & set(self.remove_changes):
            raise ValueError('Edit each draft handle once per request')
        return self


class DraftDocument(Model):
    task_id: Text
    before_id: Text
    version: int = Field(ge=0)
    changes: tuple[Change, ...] = ()

    @model_validator(mode='after')
    def unique_handles(self):
        ids = [row.change_id for row in self.changes]
        if len(ids) != len(set(ids)):
            raise ValueError('Draft change handles must be unique')
        return self


class Feedback(Model):
    problem: Text
    required_outcome: Text
    refs: tuple[Reference, ...] = Field(min_length=1)


class AssessmentResult(Model):
    explanation: Text = Field(description='Explain local graph reasonableness and preservation of original supported knowledge.')
    feedback: tuple[Feedback, ...] = Field(description='Empty only when both aspects satisfy the complete task. Otherwise concrete feedback for the same Writer; no categories or stage routing.')


class InternalReference(Model):
    """Controller-resolved evidence locator; never an LLM output schema."""
    kind: Literal['node', 'edge', 'contract']
    id: Text
    view: Literal['before', 'candidate']
    fields: tuple[str, ...] = ()


class FeedbackIssue(Model):
    issue_id: str
    refs: tuple[InternalReference, ...]
    problem: str
    required_outcome: str
    claims: tuple[dict, ...] = ()

    @property
    def basis(self):
        return ()


class AssessmentRecord(Model):
    """Controller-bound result for transaction persistence, not public output."""
    expected_version: int
    explanation: str
    issues: tuple[FeedbackIssue, ...] = ()
    candidate_hash: str | None = None

    @computed_field
    @property
    def verdict(self) -> str:
        return 'revise' if self.issues else 'accept'

    @property
    def issue_refs(self):
        return tuple(dict.fromkeys(ref for row in self.issues for ref in row.refs))

    @model_validator(mode='before')
    @classmethod
    def stored_verdict(cls, value):
        if isinstance(value, dict) and 'verdict' in value:
            value = dict(value)
            verdict = value.pop('verdict')
            if verdict != ('revise' if value.get('issues') else 'accept'):
                raise ValueError('Stored assessment acceptance disagrees with its feedback')
        return value


TOOLS = {'read_nodes': ReadNodes, 'read_relations': ReadRelations, 'read_evidence': ReadEvidence, 'read_predicates': ReadPredicates,
         'edit_draft': EditDraft}


def public_schema(model):
    return deepcopy(model.model_json_schema())


class DraftEditError(ValueError):
    def __init__(self, code, message, **details):
        super().__init__(message)
        self.details = {'code': code, **details}


def apply_edit_shape(document, payload, *, request_version=None):
    draft, edit = DraftDocument.model_validate(document), EditDraft.model_validate(payload)
    if request_version is not None and request_version != draft.version:
        raise ValueError('STALE_REQUEST: the controller-bound request addresses an old draft')
    changes = {row.change_id: row for row in draft.changes}
    for key in edit.remove_changes:
        if key not in changes:
            raise DraftEditError('UNKNOWN_CHANGE', 'Withdraw only a current draft change_id; no edit was saved', change_ids=[key], current_change_ids=list(changes))
        del changes[key]
    for row in edit.put_changes:
        if row.change_id in changes and changes[row.change_id] != row:
            raise DraftEditError('CHANGE_ALREADY_EXISTS', 'Withdraw the old entry and add its replacement with a new change_id in the same call', change_ids=[row.change_id])
        changes[row.change_id] = row
    validate_combinations(list(changes.values()))
    if changes == {row.change_id: row for row in draft.changes}:
        return draft
    return DraftDocument(task_id=draft.task_id, before_id=draft.before_id,
        version=draft.version + 1, changes=list(changes.values()))


def validate_combinations(changes):
    writes, removed, created = {}, {}, {}
    for row in changes:
        if row.kind in {'node_create', 'node_update'}:
            key = ('node', row.node_id, getattr(row, 'alternative_id', None))
        elif row.kind in {'edge_create', 'edge_update'}:
            key = ('edge', row.edge_id, None)
        else:
            key = None
        if key is not None:
            if key in writes:
                raise DraftEditError('DUPLICATE_WRITE', 'Withdraw the previous draft entry before adding its replacement', object_id=key[1], change_ids=[writes[key], row.change_id])
            writes[key] = row.change_id
        if row.kind in {'node_create', 'edge_create'}:
            ident = row.node_id if row.kind == 'node_create' else row.edge_id
            if ident in created: raise DraftEditError('DUPLICATE_CREATE', 'New object IDs must be distinct', object_id=ident, change_ids=[created[ident], row.change_id])
            created[ident] = row.change_id
        if row.kind in {'edge_replace', 'edge_remove'}:
            if row.edge_id in removed:
                raise DraftEditError('MULTIPLE_EDGE_DISPOSITIONS', 'Withdraw the obsolete operation in remove_changes before adding the replacement; no edit was saved', edge_id=row.edge_id, change_ids=[removed[row.edge_id], row.change_id])
            removed[row.edge_id] = row.change_id
    if set(removed) & ({key[1] for key in writes if key[0] == 'edge'} | set(created)):
        raise ValueError('Removed edges cannot also be created or updated')
    if any(row.kind == 'edge_replace' and row.replacement_edge_id in removed for row in changes):
        raise ValueError('A replacement must survive the complete atomic draft; no replacement chains')
