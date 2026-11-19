"""Versioned local task state; hashes and revisions are controller-owned."""

from __future__ import annotations

from copy import deepcopy
import ast
from dataclasses import asdict, dataclass, replace
from hashlib import sha256
from importlib.resources import files
import json
from typing import Any
from types import SimpleNamespace

from cortex.schema import GraphState

TASK_STATE_VERSION = 3
TOOL_CONTRACT_VERSION = "merge-patch-v3-source-units"
REVIEW_POLICY_VERSION = "accepted-input-preservation-v2"


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
    names = ("integration_identity.md", "incremental_consolidate.md", "incremental_node_assess.md",
             "incremental_content_shared.md", "incremental_relation_shared.md",
             "incremental_relation_write.md", "incremental_relation_assess.md", "incremental_protocol.md",
             "conflict_contract.md")
    prompts = {name: root.joinpath(name).read_text() for name in names}
    # On-demand guidance is model-visible policy too; its changes must not
    # silently reuse an earlier candidate-bound review.
    guidance_root = files('cortex.capabilities.sql_training.llm')
    prompts['asset_guidance'] = guidance_root.joinpath('prompts/asset_contract.py').read_text()
    prompts['operation_guidance'] = guidance_root.joinpath('generation_rule_taxonomy.py').read_text()
    if tool_schemas is None:
        from cortex.capabilities.graph_merge.integration.models import IdentityDecision
        from cortex.capabilities.graph_merge.integration.patch import MergePatch
        from cortex.capabilities.graph_merge.integration.review import Review
        from cortex.capabilities.graph_merge.llm.task_tools import build_read_tools

        # Building the read-tool registry does not execute a graph read. Hash
        # exactly its advertised descriptions/schemas, plus the public results.
        registry = build_read_tools(SimpleNamespace(evidence=()), {})
        tool_schemas = {name: {"description": tool.description,
                              "parameters": tool.args_schema.model_json_schema()}
                        for name, tool in registry.items()}
        tool_schemas.update({"IdentityDecision": IdentityDecision.model_json_schema(),
                             "MergePatch": MergePatch.model_json_schema(), "Review": Review.model_json_schema()})
        # The final tool lives inside the task loop; extract its actual public
        # docstring without importing that loop back into checkpoint code.
        source = files("cortex.capabilities.graph_merge.llm").joinpath("task_agent.py").read_text()
        final_tool = next(node for node in ast.walk(ast.parse(source))
                          if isinstance(node, ast.FunctionDef) and node.name == "submit_patch")
        tool_schemas["submit_patch"] = {"description": ast.get_docstring(final_tool),
                                         "parameters": MergePatch.model_json_schema()}
        tool_schemas['source_unit_adapter'] = files('cortex.capabilities.graph_merge.integration').joinpath('source_units.py').read_text()
        tool_schemas['guidance_adapter'] = files('cortex.capabilities.graph_merge.llm').joinpath('task_tools.py').read_text()
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
    issues: tuple[dict, ...] = ()
    semantic_issues: tuple[dict, ...] = ()
    semantic_issue_basis: tuple[dict, ...] = ()
    review: dict | None = None
    required_context: tuple[dict, ...] = ()
    recovery: str | None = None
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
            raise ValueError("Task draft must be an object")
        revision = self.draft_revision + 1
        token = canonical_hash([self.task_id, self.base_hash, revision, draft])
        return replace(self, draft=draft, draft_id=token, draft_revision=revision,
                       stage="validating", issues=(), review=None, required_context=(), recovery=None)

    def with_candidate(self, candidate):
        graph = GraphState.model_validate(candidate).model_dump(mode="json")
        return replace(self, last_valid_candidate=graph, candidate_hash=canonical_hash(graph),
                       stage="assessing", issues=(), review=None, recovery=None)

    def with_issues(self, issues, *, stage="revising"):
        return replace(self, issues=tuple(_detached(list(issues))), stage=stage, review=None)

    def with_review(self, review, *, candidate_hash):
        if self.stage != "assessing" or not self.candidate_hash or candidate_hash != self.candidate_hash:
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
        result = cls(**value)
        if not result.task_id or not result.phase:
            raise ValueError("Task identity and phase are required")
        if result.stage not in {"writing", "validating", "assessing", "revising", "blocked", "ready_to_commit", "committed"}:
            raise ValueError("Unknown task stage")
        if not isinstance(result.draft_revision, int) or result.draft_revision < 0:
            raise ValueError("Invalid task draft revision")
        GraphState.model_validate(result.base)
        if canonical_hash(result.base) != result.base_hash:
            raise ValueError("Task base hash mismatch")
        if result.last_valid_candidate is not None:
            GraphState.model_validate(result.last_valid_candidate)
            if canonical_hash(result.last_valid_candidate) != result.candidate_hash:
                raise ValueError("Task candidate hash mismatch")
        elif result.candidate_hash is not None:
            raise ValueError("Candidate hash has no candidate")
        if result.draft is not None:
            expected = canonical_hash([result.task_id, result.base_hash, result.draft_revision, result.draft])
            if result.draft_id != expected:
                raise ValueError("Task draft hash mismatch")
        elif result.draft_id is not None:
            raise ValueError("Draft id has no draft")
        if result.review is not None and result.review.get("candidate_hash") != result.candidate_hash:
            raise ValueError("Stored review addresses a different candidate")
        if result.stage in {"ready_to_commit", "committed"} and (
            not result.candidate_hash or not result.review or result.review.get("verdict") != "accept" or result.issues or result.semantic_issues
        ):
            raise ValueError("A committed task requires an accepted current review")
        _detached(result.to_dict())
        return result
