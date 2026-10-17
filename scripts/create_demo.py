"""Create a tiny synthetic SQLite dataset for installation and pipeline checks."""

import json
import sqlite3
from pathlib import Path


def create(root: Path = Path("data")) -> None:
    database = root / "databases/demo/demo.sqlite"
    database.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(database) as connection:
        connection.executescript("""
            DROP TABLE IF EXISTS orders;
            CREATE TABLE orders (id INTEGER PRIMARY KEY, region TEXT, revenue REAL);
            INSERT INTO orders VALUES (1, 'North', 100), (2, 'North', 150), (3, 'South', 80);
        """)
    train = [
        {
            "source_id": "train-1",
            "question": "What is total revenue in each region?",
            "sql": "SELECT region, SUM(revenue) FROM orders GROUP BY region",
        }
    ]
    dev = [
        {
            "source_id": "dev-1",
            "question": "What is the total revenue for North?",
            "sql": "SELECT SUM(revenue) FROM orders WHERE region = 'North'",
        }
    ]
    for name, rows in (("train", train), ("dev", dev)):
        (root / f"{name}.qa_sql.jsonl").write_text(
            "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
        )
    (root / "dev.json").write_text(
        json.dumps([{**row, "db_id": "demo"} for row in dev], indent=2) + "\n",
        encoding="utf-8",
    )
    print("Synthetic demo created under data/.")


if __name__ == "__main__":
    create()
