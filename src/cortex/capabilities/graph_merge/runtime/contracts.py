"""Bind current schemas, prompts and all runtime source to checkpoint policy."""
from __future__ import annotations

import ast
from importlib.resources import files
from types import SimpleNamespace

from cortex.capabilities.graph_merge.runtime.state import (
    ContractVersions, REVIEW_POLICY_VERSION, TOOL_CONTRACT_VERSION, canonical_hash,
)
from cortex.schema import GraphState

def implementation_sources():
    """Hash all current implementation sources, including permissions and presentation."""
    root = files('cortex.capabilities.graph_merge')
    def walk(directory, prefix=''):
        for child in sorted(directory.iterdir(), key=lambda entry: entry.name):
            relative = prefix + child.name
            if child.is_dir() and child.name != '__pycache__':
                yield from walk(child, relative + '/')
            elif child.name.endswith('.py'):
                yield relative, child.read_text()
    return dict(walk(root))


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
        from cortex.capabilities.graph_merge.identity.contract import IdentityDecision
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
        tool_schemas.update({'implementation:' + name: source for name, source in implementation_sources().items()})
    return ContractVersions(schema=canonical_hash({"ontology": schema.model_dump(mode="json"),
                                                  "graph_state": GraphState.model_json_schema()}),
                            tools=canonical_hash({"version": TOOL_CONTRACT_VERSION, "schemas": tool_schemas}),
                            prompts=canonical_hash(prompts), review_policy=review_policy)

