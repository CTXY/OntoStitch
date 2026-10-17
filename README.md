# AutoOnto for NL2SQL

AutoOnto learns ontology knowledge from question–SQL examples, integrates it into
an ontology graph, and retrieves guidance for natural-language-to-SQL generation.
The repository includes training, graph integration, online guidance, SQL generation
and execution evaluation. Python 3.11 or newer is required; the dependency versions
in `requirements.txt` were recorded on Python 3.11.

## Install

The commands below use Bash or Zsh. Run them from the repository root and keep the
virtual environment active for subsequent commands:

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt -e . -e ./baseline
cortex --help
cortex-nl2sql --help
```

Both packages share this environment. `src/cortex/` contains training, integration,
retrieval and guidance; `baseline/` contains SQL generation and evaluation.
`configs/cortex.yaml` configures Cortex, and `baseline/configs/example.yaml`
configures the standalone baseline. `ablation_study/` contains extraction and
integration variants. Markdown prompt files under the source directories are
runtime resources and must remain with the code.

## Configure

```bash
cp .env.example .env
# Edit .env: set your chat model, embedding model, endpoint and API key.
set -a
source .env
set +a
```

Use an endpoint compatible with the chat and embedding adapters. Embeddings use
the same endpoint and credentials as chat. When an embedding model is configured,
that model must be available at the endpoint. To disable optional embeddings, set
`llm.embedding.model: null` in `configs/cortex.yaml` and leave the fallback
`services.retrieve_db.embedding_model` unset.

The default configuration also requires `configs/sql-training.keys`, with one API
key per line. For a single credential, initialize it from the exported key:

```bash
(umask 077; printf '%s\n' "$CORTEX_LLM_API_KEY" > configs/sql-training.keys)
chmod 600 configs/sql-training.keys
```

The key pool is used for training and simulated-user evaluation. The standalone
baseline uses `CORTEX_LLM_API_KEY`. Configure worker counts and per-stage model
profiles in `configs/cortex.yaml`. Cortex CLI commands and `scripts/preflight.py`
read exported environment variables; they do not automatically load `.env`.
The guided evaluation wrapper and baseline commands also accept `--env-file`;
already exported values take precedence over values in that file.

## Synthetic quick start

Create a small synthetic SQLite dataset without calling a model:

```bash
python scripts/create_demo.py
```

This writes `data/databases/demo/demo.sqlite`, `data/train.qa_sql.jsonl`,
`data/dev.qa_sql.jsonl` and `data/dev.json`. Re-running it resets the demo `orders`
table and overwrites the sample input files. The default configurations use these
paths. After model configuration, check connectivity and train:

```bash
python scripts/preflight.py --config configs/cortex.yaml
cortex sql-train --config configs/cortex.yaml \
  --training-set data/train.qa_sql.jsonl --output workspace/demo/training
```

Preflight executes `SELECT 1` and makes a chat request using one randomly selected
pool key. It does not check every credential, embedding calls or all model stages.
Training and the evaluation below make model requests.

Training prints a JSON object whose `output` field identifies the dated run
directory. It writes `result.json`, `case_review.jsonl`, `graph_state.json`,
`graph.json` and `graph.html` for a non-failed run, along with detailed artifacts.
Exit status 2 means review is required; inspect the result and case review files
before using the graph. A failed run does not produce the final graph export.

Set `TRAINING_RUN` to the actual `output` value, replacing the example timestamp:

```bash
TRAINING_RUN="workspace/demo/training/20000101-120000-000"
python scripts/run_guided_evaluation.py --database demo \
  --graph-state "$TRAINING_RUN/graph_state.json" \
  --cases data/dev.qa_sql.jsonl --database-root data/databases \
  --config configs/cortex.yaml --env-file .env --output workspace/demo/online
```

The evaluation output directory must not already exist. Choose a new `--output`
for each run. The wrapper requires nonempty terminal guidance for every input case
before starting SQL generation. Outputs include `guidance/online_results.json`,
`guidance/guidance_cases.json`, `final_results/predictions.jsonl`,
`final_results/report.json` and `summary.json`.

Training uses gold SQL as learning evidence. During automated online evaluation,
the simulator receives gold SQL after interaction questions have been generated;
online retrieval and guidance do not receive that gold SQL. The execution evaluator
uses gold SQL separately. This is a simulated-user evaluation workflow; for
interactive guidance without gold SQL, inspect `cortex online-context run --help`
and `cortex online-context resume --help`.

To run SQL generation and evaluation without ontology guidance:

```bash
cortex-nl2sql preflight --config baseline/configs/example.yaml --env-file .env
cortex-nl2sql evaluate --config baseline/configs/example.yaml --env-file .env
```

For the included SQLite benchmark workflow, execution accuracy compares full row
sets. This is the only evaluation standard used in training, repair, replay and
baseline evaluation. Row order, duplicate multiplicity and column aliases are
ignored; column order and values still matter. Extra columns and approximate
numeric matches do not count as equal. Per-record evaluation overrides are not supported. Model responses and service changes can affect reproducibility.

## BIRD or SPIDER inputs

Obtain benchmark splits and databases separately. Process one database at a time;
replace the example ID and source paths below:

```bash
DB_ID="your_database_id"
python scripts/prepare_public_inputs.py \
  --train /path/to/train.json --dev /path/to/dev.json \
  --database "$DB_ID" --output data
```

The adapter accepts BIRD `SQL` and SPIDER `query`, preserves evidence and available
source IDs, and writes `train.qa_sql.jsonl`, `dev.qa_sql.jsonl` and `dev.json`.
Both input splits must contain records for the selected database. Each JSONL record
contains `source_id`, `question`, `sql` and optional `evidence`; IDs must be unique
within a split.

Place the SQLite database at `data/databases/$DB_ID/$DB_ID.sqlite` and set
`database.path` in `configs/cortex.yaml` to that actual path. Train with the same
command using a separate output directory, then pass `--database "$DB_ID"` and the
matching graph to the guided evaluation wrapper. It accepts these normalized inputs
for either benchmark. Optional BIRD column descriptions can be supplied with
`--database-description /path/to/database_description`.

Cortex paths in `configs/cortex.yaml` resolve relative to the repository root.
Paths in `baseline/configs/example.yaml` resolve relative to that configuration's
directory, which is why its data paths start with `../../`. Shell arguments resolve
relative to the current working directory. Keep graph, cases and database contents
aligned for each run.

## Additional entry points

| Script | Purpose |
| --- | --- |
| `scripts/create_demo.py` | Generate the synthetic dataset |
| `scripts/prepare_public_inputs.py` | Normalize BIRD/SPIDER inputs |
| `scripts/preflight.py` | Check the training database and chat model |
| `scripts/run_sql_training_premerge.py` | Train without graph integration |
| `scripts/merge_training_output.py` | Integrate a pre-merge workspace into an empty graph |
| `scripts/evaluate_online_context.py` | Run simulated-user online guidance without downstream SQL generation |
| `scripts/run_guided_evaluation.py` | Run guidance, downstream SQL generation and execution evaluation |
| `scripts/run_bird_dev_online_guided_baseline.py` | BIRD batch runner and shared implementation used by the generic evaluation wrapper |

For separate training and integration:

```bash
python scripts/run_sql_training_premerge.py --config configs/cortex.yaml \
  --training-set data/train.qa_sql.jsonl --output workspace/demo/premerge
# Set this to the dated workspace printed by the preceding command.
PREMERGE_RUN="workspace/demo/premerge/20000101-120000-000"
python scripts/merge_training_output.py --training-workspace "$PREMERGE_RUN" \
  --config configs/cortex.yaml --output workspace/demo/merged
```

The merge output directory must be new. Its `graph_state.json` can be passed to the
same evaluation wrapper. Use each command's `--help` for supported options; the
BIRD batch runner expects per-database inputs under `data/bird/dev` and training
runs under `workspace/bird/training`; supply explicit paths for your dataset layout.

Credentials, datasets, generated workspaces and results are excluded by
`.gitignore`. Historical results, tests and development documentation are not
bundled with this source release.
