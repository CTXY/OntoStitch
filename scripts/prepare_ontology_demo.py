"""Register real BIRD SQLite files for the local workbench without changing them."""
import argparse
import hashlib
import json
from pathlib import Path
import sqlite3

from cortex.web.graphs import load_graph


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-root", type=Path, required=True, help="Directory containing financial/financial.sqlite and thrombosis_prediction/thrombosis_prediction.sqlite")
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parents[1] / "data/web_databases")
    parser.add_argument("--artifact-root", type=Path, help="Optional BIRD/autoonto archive directory to regenerate compatible bundled graphs")
    parser.add_argument("--examples-output", type=Path, default=Path(__file__).resolve().parents[1] / "examples/ontology")
    args = parser.parse_args()
    for name in ("financial", "thrombosis_prediction"):
        source = args.database_root / name / f"{name}.sqlite"
        if not source.is_file():
            parser.error(f"Missing {source}")
        target = args.output / name / source.name
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            print(f"Already registered: {target}")
            continue
        with sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True) as original:
            with sqlite3.connect(target) as snapshot:
                original.backup(snapshot)
        print(f"Registered {name}: {target}")
    if args.artifact_root:
        args.examples_output.mkdir(parents=True, exist_ok=True)
        manifest = {}
        for name in ("financial", "thrombosis_prediction"):
            source = args.artifact_root / name / "merged_graph/graph_state.json"
            raw = source.read_bytes()
            converted = load_graph(json.loads(raw))
            (args.examples_output / f"{name}.graph.json").write_text(json.dumps(converted, ensure_ascii=False, indent=2), encoding="utf-8")
            pairs = [json.loads(line) for line in (args.artifact_root / name / "inputs/evaluation_cases.jsonl").read_text().splitlines() if line.strip()]
            (args.examples_output / f"{name}.pairs.jsonl").write_text("".join(json.dumps({k: row[k] for k in ("source_id", "question", "sql", "evidence") if k in row}, ensure_ascii=False) + "\n" for row in pairs), encoding="utf-8")
            manifest[name] = {"source": f"{name}/merged_graph/graph_state.json", "source_sha256": hashlib.sha256(raw).hexdigest(), "schema_hash": converted["schema_hash"], "nodes": len(converted["state"]["graph"]["nodes"]), "edges": len(converted["state"]["graph"]["edges"]), "conflicts": sum(n.get("merge_status") == "conflict" for n in converted["state"]["graph"]["nodes"])}
        (args.examples_output / "provenance.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
