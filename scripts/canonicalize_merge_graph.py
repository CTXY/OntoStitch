"""Export a canonical physical-ID graph and mapping into a new directory."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import tempfile

from cortex.capabilities.sql_training.physical_catalog import (
    canonicalize_physical_graph,
)
from cortex.schema import GraphSpace, GraphState, load_root_schema, validate_graph_state


def export_graph(source: Path, output: Path):
    source, output = source.resolve(), output.resolve()
    if output.exists():
        raise ValueError("Export requires a new output directory")
    raw = source.read_bytes()
    data = json.loads(raw)
    state = (
        GraphState.model_validate(data)
        if "graph" in data
        else GraphState(graph=GraphSpace.model_validate(data))
    )
    normalized, mapping = canonicalize_physical_graph(state)
    validate_graph_state(normalized, load_root_schema())
    report = {
        "source": str(source),
        "source_sha256": hashlib.sha256(raw).hexdigest(),
        "operation": "canonical_physical_ids",
        "node_mapping": mapping,
        "renamed_nodes": sum(old != new for old, new in mapping.items()),
        "nodes_before": len(state.graph.nodes),
        "nodes_after": len(normalized.graph.nodes),
        "edges_before": len(state.graph.edges),
        "edges_after": len(normalized.graph.edges),
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".canonical-graph-", dir=output.parent))
    try:
        graph_path = temporary / "merged_state.json"
        graph_path.write_text(normalized.model_dump_json(indent=2) + "\n")
        report["graph_sha256"] = hashlib.sha256(graph_path.read_bytes()).hexdigest()
        (temporary / "migration.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n"
        )
        if source.read_bytes() != raw:
            raise ValueError("Source graph changed during export")
        temporary.rename(output)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = export_graph(args.source, args.output)
    print(
        json.dumps(
            {"output": str(args.output), "renamed_nodes": report["renamed_nodes"]}
        )
    )


if __name__ == "__main__":
    main()
