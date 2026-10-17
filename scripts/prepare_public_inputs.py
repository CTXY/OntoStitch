"""Normalize user-supplied BIRD/SPIDER splits; do not redistribute datasets."""

import argparse
import json
from pathlib import Path


def prepare(train: Path, dev: Path, database: str, output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    for name, path in (("train", train), ("dev", dev)):
        rows = json.loads(path.read_text(encoding="utf-8"))
        selected = []
        for index, row in enumerate(rows):
            if row.get("db_id", row.get("database_id")) != database:
                continue
            sql = row.get("SQL", row.get("query", row.get("sql")))
            if not sql or not row.get("question"):
                raise ValueError(f"invalid {name} record at index {index}")
            selected.append(
                {
                    "source_id": str(
                        row.get("source_id", row.get("question_id", f"{name}-{index}"))
                    ),
                    "question": row["question"],
                    "sql": sql,
                    "evidence": row.get("evidence") or None,
                }
            )
        if not selected:
            raise ValueError(f"no {name} records for database {database}")
        ids = [row["source_id"] for row in selected]
        if len(ids) != len(set(ids)):
            raise ValueError(f"duplicate {name} source IDs")
        (output / f"{name}.qa_sql.jsonl").write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in selected),
            encoding="utf-8",
        )
        if name == "dev":
            (output / "dev.json").write_text(
                json.dumps(
                    [{**row, "db_id": database} for row in selected], ensure_ascii=False, indent=2
                )
                + "\n",
                encoding="utf-8",
            )
    print(json.dumps({"database": database, "output": str(output)}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--train", type=Path, required=True)
    parser.add_argument("--dev", type=Path, required=True)
    parser.add_argument("--database", required=True)
    parser.add_argument("--output", type=Path, default=Path("data"))
    args = parser.parse_args()
    prepare(args.train, args.dev, args.database, args.output)
