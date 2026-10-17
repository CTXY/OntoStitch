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

## Core interfaces

Examples below assume one run under each training output directory. If you have
multiple runs, pass the path to the specific run instead of using `*`.

### SQL training

`sql-train` extracts knowledge and merges it into the graph in one command:

```bash
cortex sql-train --config configs/cortex.yaml \
  --training-set data/train.qa_sql.jsonl --output workspace/train
```

| Parameter | Required | Description / default |
| --- | --- | --- |
| `--training-set PATH` | Yes | Training records in JSONL or JSON format |
| `--output DIR` | Yes | Parent directory for the generated run directory |
| `--config PATH` | No | Configuration; default `configs/cortex.yaml` |
| `--schema PATH` | No | Schema JSON; defaults to the configured root schema |
| `--graph-state DIR` | No | Persistent graph bundle to load and update; defaults inside the new run directory |

The command prints the run path in its `output` field and exports
`graph_state.json` there. `--graph-state` is the persistent bundle directory used
for incremental training, not the exported `graph_state.json` file. Exit code 2
means the training result needs review.

### Separate training and graph merge

To run extraction and graph integration separately:

```bash
python scripts/run_sql_training_premerge.py --config configs/cortex.yaml \
  --training-set data/train.qa_sql.jsonl --output workspace/premerge

python scripts/merge_training_output.py \
  --training-workspace workspace/premerge/* \
  --config configs/cortex.yaml --output workspace/merged
```

The pre-merge command requires all three flags: `--config`, `--training-set`
(JSONL), and `--output` (parent directory). It writes `incoming_state.json` and
case artifacts inside a generated run directory.

The merge command has these parameters:

| Parameter | Required | Description / default |
| --- | --- | --- |
| `--training-workspace DIR` | Yes | One pre-merge run containing `incoming_state.json` and its evidence artifacts |
| `--output DIR` | Yes | New output directory; must not already exist |
| `--config PATH` | No | Configuration; default `configs/cortex.yaml` |

This command merges the incoming state into an empty graph and writes
`workspace/merged/graph_state.json`. It has no `--existing-state` option.

### Online context

Run guidance for one question using the graph from the quick start:

```bash
cortex online-context run "What is the total revenue for North?" \
  --graph-state workspace/train/*/graph_state.json \
  --config configs/cortex.yaml --output workspace/online.json
```

For the separate merge workflow, use `workspace/merged/graph_state.json` instead.

| Parameter | Required | Description / default |
| --- | --- | --- |
| `QUESTION` | Yes | Positional natural-language question |
| `--graph-state PATH` | Yes | Exported `graph_state.json` or a run directory containing that file |
| `--config PATH` | No | Configuration; default `configs/cortex.yaml` |
| `--initial-sql TEXT` | No | Initial candidate SQL |
| `--question-budget INTEGER` | No | Maximum clarification questions, including JOIN questions; defaults to configuration |
| `--schema-context DIR` | No | BIRD-style column description directory; alias `--column-context-dir` |
| `--output PATH` | No | Save the result JSON; the result is also printed to stdout |
| `--trace-output PATH` | No | Save organizer trace JSON |

If the result has `status: needs_user_input`, read `interaction_questions` and
create `workspace/answers.json` using the returned question and option IDs:

```json
[
  {
    "question_id": "question-1",
    "selected_option_ids": ["option-1"],
    "explanation": ""
  }
]
```

Select one or more option IDs, or use an empty list with a nonempty explanation.
Resume using the original result file, which already contains `pending`:

```bash
cortex online-context resume \
  --graph-state workspace/train/*/graph_state.json \
  --pending workspace/online.json --answers workspace/answers.json \
  --config configs/cortex.yaml --output workspace/resolved.json
```

| Parameter | Required | Description / default |
| --- | --- | --- |
| `--graph-state PATH` | Yes | Same graph used for the pending request |
| `--pending PATH` | Yes | Result JSON containing `pending`, or a JSON file containing just that state |
| `--answers PATH` | Yes | Answer array as above, or an object with an `answers` array |
| `--config PATH` | No | Configuration; default `configs/cortex.yaml` |
| `--output PATH` | No | Save the resumed result JSON; also printed to stdout |

Repeat if another clarification round is returned. The final result contains
`downstream_guidance.guidance_content` for use in SQL generation. Online context
produces guidance; the guided evaluation command in the quick start also generates
SQL and evaluates it.

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
