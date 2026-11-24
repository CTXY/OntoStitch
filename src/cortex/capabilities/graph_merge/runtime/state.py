"""Versioned local task state; hashes and revisions are controller-owned."""

from __future__ import annotations

import ast
import json
from copy import deepcopy
from dataclasses import asdict, dataclass, field, replace
from hashlib import sha256
from importlib.resources import files
from types import SimpleNamespace
from typing import Any

from cortex.schema import GraphState


_SOURCE_PATHS = {'integration/signal_scope_migration.py': 'reconciliation/scope_migration.py', 'identity.py': 'identity/legacy.py', 'integration/incremental.py': 'engine.py', 'integration/context.py': 'inputs.py', 'integration/batch.py': 'graph/batch.py', 'integration/catalog.py': 'identity/catalog.py', 'integration/compiler.py': 'graph/publication.py', 'integration/conflict_resolution.py': 'consolidation/conflicts.py', 'integration/contracts.py': 'graph/contracts.py', 'integration/edit_feedback.py': 'runtime/feedback.py', 'integration/evidence_refs.py': 'evidence.py', 'integration/lineage.py': 'graph/lineage.py', 'integration/local_validation.py': 'graph/validation.py', 'integration/models.py': 'graph/edit_contract.py', 'integration/node_context.py': 'consolidation/context.py', 'integration/node_relation_repair.py': 'consolidation/coordination.py', 'integration/node_relations.py': 'consolidation/relations.py', 'integration/node_review.py': 'consolidation/assessment.py', 'integration/node_revision.py': 'consolidation/revision.py', 'integration/packing.py': 'llm/packing.py', 'integration/patch.py': 'consolidation/compiler.py', 'integration/proposal_scope.py': 'graph/scope.py', 'integration/provenance.py': 'graph/provenance.py', 'integration/receipt_ledger.py': 'reconciliation/ledger.py', 'integration/reconciliation_compile.py': 'reconciliation/compiler.py', 'integration/reconciliation_contract.py': 'reconciliation/contract.py', 'integration/relation_context.py': 'reconciliation/context.py', 'integration/relation_edits.py': 'graph/edits.py', 'integration/relation_followups.py': 'reconciliation/followups.py', 'integration/relation_merger.py': 'reconciliation/relation_merger.py', 'integration/relation_merger_stage.py': 'runtime/relation_stage.py', 'integration/review.py': 'consolidation/review.py', 'integration/signal_effects.py': 'graph/impact.py', 'integration/signal_premises.py': 'reconciliation/premises.py', 'integration/signal_receipts.py': 'reconciliation/receipts.py', 'integration/signal_scope.py': 'reconciliation/scope.py', 'integration/signals.py': 'reconciliation/inspection.py', 'integration/single_graph.py': 'reconciliation/single_graph.py', 'integration/source_units.py': 'graph/source_units.py', 'integration/task_context.py': 'consolidation/task_context.py', 'integration/task_runner.py': 'runtime/task.py', 'integration/task_state.py': 'runtime/state.py', 'integration/tools.py': 'identity/tools.py', 'integration/validation_repair.py': 'graph/repair.py', 'integration/workspace.py': 'runtime/workspace.py', 'llm/identity_guidance.py': 'identity/guidance.py', 'llm/node_context.py': 'consolidation/presentation.py', 'llm/node_contract.py': 'consolidation/contract.py', 'llm/reconciliation_agent.py': 'reconciliation/agent.py', 'llm/reconciliation_context.py': 'reconciliation/permissions.py', 'llm/reconciliation_presentation.py': 'reconciliation/presentation.py', 'llm/reconciliation_projection.py': 'reconciliation/projection.py', 'llm/reconciliation_prompts.py': 'reconciliation/policy.py', 'llm/task_agent.py': 'llm/agent.py', 'llm/input_budget.py': 'llm/budget.py', 'llm/relation_reads.py': 'reconciliation/reads.py'}

def source_file(namespace, name):
    """Resolve implementation dependencies used by conservative policy hashes."""
    relative = namespace + '/' + name
    return files('cortex.capabilities.graph_merge').joinpath(_SOURCE_PATHS.get(relative, relative))


TASK_STATE_VERSION = 4
TOOL_CONTRACT_VERSION = "single-graph-reconciliation-v6"
REVIEW_POLICY_VERSION = "local-graph-preservation-v7-direct-result"


class CheckpointContractMismatch(ValueError):
    """A checkpoint needs an explicit migration before the current runtime."""


def canonical_hash(value: Any) -> str:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return sha256(encoded.encode()).hexdigest()


def _detached(value):
    """Validate JSON before storage and return a detached value."""
    def encode(item):
        if hasattr(item, "model_dump"):
            return item.model_dump(mode="json")
        raise TypeError(f"Task value is not JSON serializable: {type(item).__name__}")

    return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False, default=encode))


@dataclass(frozen=True)
class ContractVersions:
    schema: str
    tools: str
    prompts: str
    review_policy: str
    task_state: int = TASK_STATE_VERSION

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, value):
        result = cls(**value)
        if result.task_state != TASK_STATE_VERSION:
            raise CheckpointContractMismatch(f"Unsupported task-state version {result.task_state}")
        if not all(isinstance(getattr(result, name), str) and getattr(result, name)
                   for name in ("schema", "tools", "prompts", "review_policy")):
            raise CheckpointContractMismatch("Checkpoint contract versions must be nonempty strings")
        return result


def build_contract_versions(schema, *, tool_schemas=None, review_policy=REVIEW_POLICY_VERSION):
    """Hash actual prompt assets; callers may supply registered public tool schemas."""
    root = files("cortex.capabilities.graph_merge.llm.prompts")
    names = ("integration_identity.md", "incremental_protocol.en.md")
    prompts = {name: root.joinpath(name).read_text() for name in names}
    prompts["schema-contracts.en.json"] = root.joinpath("schema-contracts.en.json").read_text()
    from cortex.capabilities.graph_merge.reconciliation.policy import prompt_assets
    prompts.update({"reconciliation/" + name: body for name, body in prompt_assets().items()})
    relation_policy = root.joinpath('relation_merger', 'writer.en.md')
    if relation_policy.is_file():
        prompts['relation_merger/writer.en.md'] = relation_policy.read_text()
    from cortex.capabilities.graph_merge.consolidation.contract import node_prompt_modules, node_output_model, build_node_read_tools
    for role in ('writer', 'assessment'):
        for kind in ('concept', 'metric', 'event', 'generation_rule'):
            for name in node_prompt_modules(role, kind):
                prompts['node_local/' + name] = root.joinpath('node_local', name).read_text()
    # On-demand guidance is model-visible policy too; its changes must not
    # silently reuse an earlier candidate-bound review.
    guidance_root = files('cortex.capabilities.sql_training.llm')
    prompts['asset_guidance'] = guidance_root.joinpath('prompts/asset_contract.py').read_text()
    prompts['operation_guidance'] = guidance_root.joinpath('generation_rule_taxonomy.py').read_text()
    if tool_schemas is None:
        from cortex.capabilities.graph_merge.graph.edit_contract import IdentityDecision
        from cortex.capabilities.graph_merge.reconciliation.contract import TOOLS, AssessmentResult
        tool_schemas = {"reconciliation:" + name: model.model_json_schema() for name, model in TOOLS.items()}
        tool_schemas['reconciliation:assessment_result'] = AssessmentResult.model_json_schema()
        tool_schemas["IdentityDecision"] = IdentityDecision.model_json_schema()
        # The final tool lives inside the task loop; extract its actual public
        # docstring without importing that loop back into checkpoint code.
        source = files("cortex.capabilities.graph_merge.llm").joinpath("agent.py").read_text()
        final_tool = next(node for node in ast.walk(ast.parse(source))
                          if isinstance(node, ast.FunctionDef) and node.name == "submit_patch")
        tool_schemas["node_submit_patch"] = {"description": ast.get_docstring(final_tool),
                                         "parameters": node_output_model('writer').model_json_schema()}
        tool_schemas['source_unit_adapter'] = files('cortex.capabilities.graph_merge.graph').joinpath('source_units.py').read_text()
        local_reads = build_node_read_tools(SimpleNamespace(evidence=()), {'allowed_evidence_ids': []})
        tool_schemas['node_local_reads'] = {name: {'description': tool.description,
            'parameters': tool.args_schema.model_json_schema()} for name, tool in local_reads.items()}
        tool_schemas['node_local_results'] = {role: node_output_model(role).model_json_schema() for role in ('writer', 'assessment')}
        for name in ('identity_guidance.py', 'prompt_language.py', 'node_contract.py', 'node_context.py', 'packets.py', 'reconciliation_context.py', 'reconciliation_agent.py', 'reconciliation_prompts.py', 'reconciliation_projection.py', 'reconciliation_presentation.py'):
            tool_schemas[name] = source_file('llm', name).read_text()
        for name in ('single_graph.py', 'local_validation.py', 'semantic_notes.py', 'patch.py', 'node_revision.py', 'node_relations.py', 'node_relation_repair.py', 'relation_followups.py', 'reconciliation_contract.py', 'reconciliation_compile.py', 'signals.py', 'signal_premises.py', 'signal_scope.py', 'signal_effects.py', 'signal_scope_migration.py', 'signal_receipts.py', 'receipt_ledger.py'):
            tool_schemas[name] = source_file('integration', name).read_text()
        for namespace, names in (('integration', ('relation_merger.py', 'relation_merger_stage.py')),
                                 ('llm', ('relation_merger.py',))):
            for name in names:
                path = source_file(namespace, name)
                if path.is_file():
                    tool_schemas[namespace + '/' + name] = path.read_text()
    return ContractVersions(schema=canonical_hash({"ontology": schema.model_dump(mode="json"),
                                                  "graph_state": GraphState.model_json_schema()}),
                            tools=canonical_hash({"version": TOOL_CONTRACT_VERSION, "schemas": tool_schemas}),
                            prompts=canonical_hash(prompts), review_policy=review_policy)


def validate_checkpoint_contract(snapshot, expected: ContractVersions):
    """Legacy state is not silently interpreted using new draft semantics."""
    actual = snapshot.get("contract_versions")
    if actual is None:
        raise CheckpointContractMismatch("Legacy merge checkpoint requires explicit migration to a new output directory")
    parsed = ContractVersions.from_dict(actual)
    differences = [name for name, value in expected.to_dict().items() if parsed.to_dict().get(name) != value]
    if differences:
        raise CheckpointContractMismatch("Merge checkpoint contract mismatch: " + ", ".join(differences))
    task = snapshot.get("task_state")
    if task is not None:
        restored = TaskState.from_dict(task)
        if restored.versions != expected:
            raise CheckpointContractMismatch("Task and workspace contracts differ")
    return parsed


@dataclass(frozen=True)
class TaskState:
    task_id: str
    phase: str
    base: dict
    base_hash: str
    versions: ContractVersions
    stage: str = "writing"
    draft: dict | None = None
    draft_id: str | None = None
    draft_revision: int = 0
    last_valid_candidate: dict | None = None
    candidate_hash: str | None = None
    candidate_draft_id: str | None = None
    compilation_binding: str | None = None
    issues: tuple[dict, ...] = ()
    issues_draft_id: str | None = None
    semantic_issues: tuple[dict, ...] = ()
    semantic_issue_basis: tuple[dict, ...] = ()
    review: dict | None = None
    required_context: tuple[dict, ...] = ()
    recovery: str | None = None
    agent_recovery: dict[str, dict] = field(default_factory=dict)
    relation_followups: tuple[dict, ...] = ()
    coupled_relation: dict | None = None
    version: int = TASK_STATE_VERSION

    @classmethod
    def capture(cls, base, *, task_id, phase, versions):
        graph = GraphState.model_validate(base).model_dump(mode="json")
        return cls(task_id=task_id, phase=phase, base=graph, base_hash=canonical_hash(graph), versions=versions)

    def with_draft(self, draft):
        if hasattr(draft, "model_dump"):
            draft = draft.model_dump(mode="json")
        draft = _detached(draft)
        if not isinstance(draft, dict):
            raise TypeError("Task draft must be an object")
        if draft == self.draft:
            return self
        revision = self.draft_revision + 1
        token = canonical_hash([self.task_id, self.base_hash, revision, draft])
        return replace(self, draft=draft, draft_id=token, draft_revision=revision,
                       stage="validating", issues=(), issues_draft_id=None, review=None, recovery=None,
                       relation_followups=(), coupled_relation=None,
                       compilation_binding=None)

    def with_candidate(self, candidate, *, submitted=True, compilation_binding=None):
        if self.draft_id is None:
            raise ValueError("A candidate requires a current task draft")
        graph = GraphState.model_validate(candidate).model_dump(mode="json")
        return replace(self, last_valid_candidate=graph, candidate_hash=canonical_hash(graph),
                       candidate_draft_id=self.draft_id,
                       compilation_binding=compilation_binding,
                       stage="assessing" if submitted else "compiled_unsubmitted", issues=(),
                       issues_draft_id=None, review=None, recovery=None)

    def with_assessment_handoff(self):
        """Bind the exact compiled Writer action to independent Assessment."""
        if (self.stage not in {'compiled_unsubmitted', 'assessing'}
                or self.candidate_hash is None or self.last_valid_candidate is None
                or self.draft_id is None or self.candidate_draft_id != self.draft_id
                or not self.compilation_binding):
            raise ValueError('DRAFT_NOT_COMPILED: Assessment requires the exact current compiled draft')
        return replace(self, stage='assessing')

    def with_issues(self, issues, *, stage="revising"):
        issues = tuple(_detached(list(issues)))
        return replace(self, issues=issues, issues_draft_id=self.draft_id if issues else None,
                       stage=stage, review=None)

    def with_agent_recovery(self, key, value):
        if not isinstance(key, str) or not key:
            raise ValueError("Agent recovery key must be a nonempty string")
        records = _detached(self.agent_recovery)
        if value is None:
            records.pop(key, None)
        else:
            if not isinstance(value, dict):
                raise TypeError("Agent recovery record must be an object")
            records[key] = _detached(value)
        return replace(self, agent_recovery=records)

    def with_review(self, review, *, candidate_hash):
        if (self.stage != "assessing" or not self.candidate_hash or candidate_hash != self.candidate_hash
                or self.candidate_draft_id is None or self.candidate_draft_id != self.draft_id):
            raise ValueError("Review does not address the current candidate")
        review = _detached(review.model_dump(mode="json") if hasattr(review, "model_dump") else review)
        verdict = review.get("verdict")
        if verdict not in {"accept", "revise", "needs_context", "revisit_identity"}:
            raise ValueError("Unknown task review verdict")
        issues = tuple(review.get("issues", ()))
        if verdict == "accept" and issues:
            raise ValueError("An accepted review cannot retain issues")
        return replace(self, review={**review, "candidate_hash": candidate_hash},
                       issues=tuple(_detached(list(issues))),
                       issues_draft_id=self.draft_id if issues else None,
                       semantic_issues=(() if verdict == 'accept' else self.semantic_issues
                                        if verdict == 'needs_context' else tuple(_detached(list(issues)))),
                       semantic_issue_basis=(() if verdict == 'accept' else self.semantic_issue_basis),
                       stage=("ready_to_commit" if verdict == "accept" else
                              "assessing" if verdict == 'needs_context' else "revising"))

    def to_dict(self):
        result = asdict(self)
        result["versions"] = self.versions.to_dict()
        result["issues"] = list(self.issues)
        result['semantic_issues'] = list(self.semantic_issues)
        result['semantic_issue_basis'] = list(self.semantic_issue_basis)
        result['required_context'] = list(self.required_context)
        result['relation_followups'] = list(self.relation_followups)
        return _detached(result)

    @classmethod
    def from_dict(cls, value):
        value = deepcopy(value)
        if value.get("version") != TASK_STATE_VERSION:
            raise CheckpointContractMismatch("Unsupported task-state payload version")
        value["versions"] = ContractVersions.from_dict(value["versions"])
        value["issues"] = tuple(value.get("issues", ()))
        value['semantic_issues'] = tuple(value.get('semantic_issues', ()))
        value['semantic_issue_basis'] = tuple(value.get('semantic_issue_basis', ()))
        value['required_context'] = tuple(value.get('required_context', ()))
        value['relation_followups'] = tuple(value.get('relation_followups', ()))
        result = cls(**value)
        if not result.task_id or not result.phase:
            raise ValueError("Task identity and phase are required")
        if result.stage not in {"writing", "validating", "compiled_unsubmitted", "assessing", "revising", "blocked", "ready_to_commit", "committed"}:
            raise ValueError("Unknown task stage")
        if not isinstance(result.draft_revision, int) or result.draft_revision < 0:
            raise ValueError("Invalid task draft revision")
        for binding in (result.candidate_draft_id, result.issues_draft_id):
            if binding is not None and (not isinstance(binding, str) or not binding):
                raise ValueError("Draft bindings must be nonempty strings")
        GraphState.model_validate(result.base)
        if canonical_hash(result.base) != result.base_hash:
            raise ValueError("Task base hash mismatch")
        if result.last_valid_candidate is not None:
            GraphState.model_validate(result.last_valid_candidate)
            if canonical_hash(result.last_valid_candidate) != result.candidate_hash:
                raise ValueError("Task candidate hash mismatch")
        elif result.candidate_hash is not None:
            raise ValueError("Candidate hash has no candidate")
        if result.candidate_draft_id is not None and not result.candidate_hash:
            raise ValueError("Candidate draft binding has no candidate")
        if result.draft is not None:
            expected = canonical_hash([result.task_id, result.base_hash, result.draft_revision, result.draft])
            if result.draft_id != expected:
                raise ValueError("Task draft hash mismatch")
        elif result.draft_id is not None:
            raise ValueError("Draft id has no draft")
        if (result.candidate_draft_id is not None
                and result.stage in {"compiled_unsubmitted", "assessing", "ready_to_commit", "committed"}
                and result.candidate_draft_id != result.draft_id):
            raise ValueError("Candidate does not compile the current task draft")
        if result.stage == 'compiled_unsubmitted' and (result.candidate_draft_id is None
                or result.candidate_hash is None or result.last_valid_candidate is None
                or not result.compilation_binding):
            raise ValueError('Compiled preview requires an exact current candidate binding')
        if result.compilation_binding is not None and (not isinstance(result.compilation_binding, str)
                or not result.compilation_binding):
            raise ValueError('Compilation input binding must be a nonempty string')
        if result.issues_draft_id is not None and (
            not result.issues or result.issues_draft_id != result.draft_id
        ):
            raise ValueError("Task issues address a different draft")
        if (not isinstance(result.agent_recovery, dict)
                or any(not isinstance(key, str) or not key or not isinstance(record, dict)
                       for key, record in result.agent_recovery.items())):
            raise ValueError("Agent recovery must contain named object records")
        from cortex.capabilities.graph_merge.consolidation.contract import RelationFollowup
        for row in result.relation_followups:
            RelationFollowup.model_validate(row)
        if result.coupled_relation is not None:
            if not isinstance(result.coupled_relation, dict) or result.coupled_relation.get('draft_id') != result.draft_id:
                raise ValueError('Coupled relation repair addresses a different node draft')
        if result.review is not None and result.review.get("candidate_hash") != result.candidate_hash:
            raise ValueError("Stored review addresses a different candidate")
        if result.stage in {"ready_to_commit", "committed"} and (
            not result.candidate_hash or not result.review or result.review.get("verdict") != "accept" or result.issues or result.semantic_issues
        ):
            raise ValueError("A committed task requires an accepted current review")
        _detached(result.to_dict())
        return result
