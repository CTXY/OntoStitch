"""Merge the public pre-merge training state using the existing graph operator."""

import argparse
import json
from pathlib import Path

from cortex.bootstrap.graph_merge import build_graph_merge_operator
from cortex.capabilities.sql_training.artifacts.workspace import Workspace
from cortex.capabilities.sql_training.graph_merge_evidence import (
    SqlTrainingGraphMergeEvidenceResolver,
    WorkspaceEvidenceRepository,
)
from cortex.config import load_config
from cortex.infrastructure.artifacts import FileSystemGraphMergeTraceSink
from cortex.schema import GraphSpace, GraphState, load_root_schema, validate_graph_state


def merge(training_workspace: Path, config_path: Path, output: Path) -> None:
    config = load_config(config_path)
    schema = load_root_schema(config.root_schema.external_path)
    incoming = GraphState.model_validate_json(
        (training_workspace / "incoming_state.json").read_text(encoding="utf-8")
    )
    validate_graph_state(incoming, schema)
    output.mkdir(parents=True, exist_ok=False)
    operator = build_graph_merge_operator(
        config,
        evidence_resolver=SqlTrainingGraphMergeEvidenceResolver(
            WorkspaceEvidenceRepository(Workspace(training_workspace))
        ),
        trace_sink=FileSystemGraphMergeTraceSink(output / "llm_traces.jsonl"),
    )
    result = operator.merge(
        GraphState(graph=GraphSpace(), node_conflicts=()), incoming, schema
    )
    validate_graph_state(result.state, schema)
    (output / "graph_state.json").write_text(
        result.state.model_dump_json(indent=2) + "\n"
    )
    (output / "merge_report.json").write_text(result.model_dump_json(indent=2) + "\n")
    print(json.dumps({"output": str(output), "nodes": len(result.state.graph.nodes)}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--training-workspace", type=Path, required=True)
    parser.add_argument("--config", type=Path, default=Path("configs/cortex.yaml"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    merge(args.training_workspace.resolve(), args.config, args.output.resolve())
