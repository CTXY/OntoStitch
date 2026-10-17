# OntoStitch for NL2SQL

OntoStitch learns ontology knowledge from question–SQL examples and uses it to
guide natural-language-to-SQL generation.

## Environment

Use Python 3.11 and run the following commands from the repository root
(Bash or Zsh):

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt -e . -e ./baseline
```

## Configuration

```bash
cp .env.example .env
```

Edit these values in `.env`:

```dotenv
CORTEX_LLM_MODEL=your-chat-model
CORTEX_LLM_BASE_URL=https://api.openai.com/v1
CORTEX_LLM_API_KEY=your-api-key
CORTEX_EMBEDDING_MODEL=your-embedding-model
```

The chat and embedding models use the same endpoint and API key. Load the settings
before running the commands below:

```bash
set -a
source .env
set +a
```

Set the SQLite database path in `configs/cortex.yaml`:

```yaml
database:
  type: sqlite
  path: data/databases/demo/demo.sqlite
```

To run without embeddings, set `llm.embedding.model` to `null` in the same file.

## Quick start

Create the sample database and input files, then train:

```bash
python scripts/create_demo.py
cortex sql-train --config configs/cortex.yaml \
  --training-set data/train.qa_sql.jsonl --output workspace/train
```

Run guided SQL evaluation using the generated graph. These commands assume a new
`workspace/train` directory containing one training run:

```bash
python scripts/run_guided_evaluation.py --database demo \
  --graph-state workspace/train/*/graph_state.json \
  --cases data/dev.qa_sql.jsonl --database-root data/databases \
  --config configs/cortex.yaml --env-file .env --output workspace/eval
```

Use a new evaluation output directory for each run. Results are saved under
`workspace/eval`, including predictions and the evaluation report in `final_results/`.

## Dataset format

Training and guided evaluation use UTF-8 JSONL files, with one example per line:

```jsonl
{"source_id":"train-1","question":"What is total revenue in each region?","sql":"SELECT region, SUM(revenue) FROM orders GROUP BY region","evidence":null}
```

| Field | Required | Description |
| --- | --- | --- |
| `source_id` | Yes | Unique example ID within the input file |
| `question` | Yes | Natural-language question |
| `sql` | Yes | Reference SQL for the selected database |
| `evidence` | No | Additional context for the question |

Place each SQLite database at `data/databases/DB_ID/DB_ID.sqlite`. Process one
database at a time: set `database.path` in `configs/cortex.yaml` and use the same
`DB_ID` for the evaluation command's `--database` argument.

For BIRD or SPIDER JSON files, prepare the inputs with:

```bash
python scripts/prepare_public_inputs.py \
  --train /path/to/train.json --dev /path/to/dev.json \
  --database your_database_id --output data
```

The adapter accepts BIRD `SQL` and SPIDER `query` fields and writes
`data/train.qa_sql.jsonl`, `data/dev.qa_sql.jsonl` and `data/dev.json`.
