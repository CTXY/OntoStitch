# OntoStitch for NL2SQL

OntoStitch learns ontology knowledge from question–SQL examples, merges it into a
persistent graph, and builds request-specific guidance for downstream SQL generation.

```text
SQLite + training question/SQL pairs
  → sql-train: extraction → validation → graph merge
  → graph_state.json
  → online-context: retrieval → clarification → guidance
                               ↳ human answers OR Gold-SQL simulated answers
```

This repository distributes source code. Local datasets, `ablation_study/`,
`artifacts/`, `data/`, `datasets/`, `workspace/`, `output/`, `examples/`, `tests/`,
`docs/`, and `Dockerfile.web` are excluded. The synthetic quick start below creates
its own data locally; no downloaded dataset or excluded file is needed.

## 1. Install

Use **CPython 3.11** with SQLite support and a Bash/Zsh shell on Linux or macOS
(or Bash in WSL). Run all commands from the repository root. Network access to
your model provider and a writable local workspace are required. No PostgreSQL,
Neo4j, GPU, or external validation server is required for the SQLite workflow.

```bash
git clone https://github.com/CTXY/OntoStitch.git
cd OntoStitch
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt -e .
python -m pip check
cortex --help
```

`requirements.txt` pins the validated runtime dependencies. The optional guided
SQL evaluation baseline needs one additional local package:

```bash
python -m pip install -e ./baseline
```

## 2. Configure the model and database

```bash
cp .env.example .env
```

Fill in your own OpenAI-compatible **chat model** connection in `.env`:

```dotenv
CORTEX_LLM_MODEL=your-chat-model
CORTEX_LLM_BASE_URL=https://api.openai.com/v1
CORTEX_LLM_API_KEY=replace-with-your-own-key
LANGSMITH_TRACING=false
```

The model must support tool calling and structured JSON output. Extraction and
merge use agent tools; retrieval, assessment, guidance, and the simulated user
also use the chat model. Small-model output errors can produce `needs_review`;
a completed process does not by itself prove that every learned definition is correct.

Load the environment **in every new shell** before invoking `cortex`:

```bash
set -a
source .env
set +a
```

The CLI does not automatically load `.env`. Keep that file private.

**Embeddings are optional and disabled when `CORTEX_EMBEDDING_MODEL` is unset.**
The default retrieval path works without them. Enable them only if the same
endpoint and API key support `/embeddings` and the selected embedding model:

```dotenv
# Optional; omit this line for the simplest setup.
CORTEX_EMBEDDING_MODEL=your-embedding-model
```

An endpoint that supports chat may still reject embedding requests with 404.
To turn embeddings off after previously enabling them:

```bash
unset CORTEX_EMBEDDING_MODEL
```

Remove/comment the corresponding line in `.env` as well. Alternatively set
`llm.embedding.model: null` in YAML. No embeddings service is needed for the
quick start.

`CORTEX_LLM_ENABLE_THINKING` is an optional provider extension. Leave it unset
for the standard OpenAI API; set it to `true` or `false` only when your provider
supports the `enable_thinking` request field. It is omitted from requests unless
explicitly configured.

`configs/cortex.yaml` selects the database and runtime settings. The shipped
configuration matches the synthetic demo:

```yaml
database:
  type: sqlite
  path: data/databases/demo/demo.sqlite
services:
  sql_training:
    case_max_workers: 1
    agent_max_steps: 10
    max_rounds: 3
    max_iterations: 3
    required_consecutive_correct_replays: 3
    validation:
      dialect: sqlite
  online_context:
    checkpoint_path: workspace/checkpoints/online_context.sqlite
    questions_per_round: 1
    question_budget: null
    query_example_top_k: 0
```

| Setting | Effect |
| --- | --- |
| `database.path` | Existing SQLite database used for schema inspection and SQL validation |
| `validation.dialect` | SQL dialect for training validation; use `sqlite` for this demo |
| `required_consecutive_correct_replays` | Required consecutive SQL regeneration/execution successes before acceptance; default 3 |
| `case_max_workers` | Concurrent extraction cases; start with 1 |
| `questions_per_round` | Clarification questions presented in each round; default 1 |
| `question_budget` | Maximum questions across a request, including JOINs; `null` allows all discovered questions, 0 skips asking |
| `checkpoint_path` | Persistent Online Context SQLite checkpoint, needed by `resume` |
| `query_example_top_k` | Historical examples attached to guidance; 0 needs no historical workspace |

Relative paths inside a configuration under `configs/` resolve against the
repository root; for other config locations they resolve against the config's
parent directory. CLI input/output paths resolve against the working directory.
Process one database per training graph. Online Context must use that same graph
and database dialect. Checkpoints are created automatically in writable directories.

Optional per-stage chat model overrides belong under `llm.profiles` in YAML, e.g.:

```yaml
llm:
  profiles:
    sql_training:
      model: your-training-model
    graph_merge:
      model: your-merge-model
    retrieve_db:
      model: your-retrieval-model
    online_context:
      model: your-online-model
    simulated_user:
      model: your-simulator-model
```

These profiles share the configured endpoint and credentials. With no overrides,
all stages inherit `CORTEX_LLM_MODEL`.

## 3. Extraction → merge with `sql-train`

Generate a tiny synthetic database (three orders) and training/dev records:

```bash
python scripts/create_demo.py
```

This recreates `data/databases/demo/demo.sqlite` and the demo input files; use it
only for the demo. Real data can be supplied in the format described below.

Run the complete extraction, SQL validation, semantic merge, and graph export:

```bash
cortex sql-train \
  --config configs/cortex.yaml \
  --training-set data/train.qa_sql.jsonl \
  --output workspace/train \
  --result-file workspace/train-summary.json \
  --progress-file workspace/train-progress.jsonl
```

The command prints a terminal JSON summary. Read its exact `output` path instead
of using a wildcard, which becomes ambiguous when multiple runs exist:

```bash
TRAIN_RUN="$(python -c 'import json; print(json.load(open("workspace/train-summary.json"))["output"])')"
export TRAIN_RUN
```

Successful/review runs export the **merged graph** to:

| File under `$TRAIN_RUN` | Content |
| --- | --- |
| `graph_state.json` | Complete merged GraphState; pass this file to Online Context |
| `graph.json` | Graph nodes and edges |
| `graph.html` | Standalone graph viewer |
| `result.json` | Full extraction evaluation and merge result |
| `merge_report.json` | Merge mappings, diagnostics, and candidate state |
| `case_review.jsonl` | Case-by-case review index |
| `graph_state/` | Persistent graph repository bundle for incremental training |

Check `status`, `passed`, and `failed` in `workspace/train-summary.json`:

- **Exit 0 / `completed`**: extraction, merge, and final validation completed.
- **Exit 2 / `needs_review`**: a graph is exported, but case failures, unresolved
  conflicts, or merge diagnostics need review. Inspect `result.json` and
  `case_review.jsonl`; do not treat this as all cases passing.
- **Exit 1 / `failed`**: no new final graph is published. Inspect the error and
  local case artifacts. Fatal extraction does not invoke merge.

Training is model-dependent and may take several minutes. The progress file
shows stage and case events. Validation executes regenerated read-only SQL
against the configured database and compares it with the reference SQL.

For incremental training, pass `--graph-state workspace/graph-store` to every
`sql-train` call that should update the same persistent graph. This flag expects a
**bundle directory**. Online Context's flag with the same name expects an exported
**GraphState JSON or a run directory**.

### Optional: run extraction and merge independently

These commands use the same gates and result contract as `sql-train`:

```bash
cortex sql-extract --config configs/cortex.yaml \
  --training-set data/train.qa_sql.jsonl --output workspace/extract \
  --result-file workspace/extract-summary.json

EXTRACT_RUN="$(python -c 'import json; print(json.load(open("workspace/extract-summary.json"))["output"])')"

cortex graph-merge --config configs/cortex.yaml \
  --training-workspace "$EXTRACT_RUN" --output workspace/merged
```

`sql-extract` writes `extraction_result.json` with its evaluation, incoming graph,
base graph, and schema identity. `graph-merge` requires this complete handoff and
its evidence artifacts; a bare incoming graph is insufficient. Its final graph
is `workspace/merged/graph_state.json`. Use a fresh merge output directory for a
new run. To retry an interrupted merge, reuse its saved `merge_run_id.txt` via
`--run-id ID` with unchanged inputs, configuration, and output directory.

## 4. Online Context — setting A: human interaction

Start from the merged graph created in step 3:

```bash
cortex online-context run "Show the revenue for North." \
  --graph-state "$TRAIN_RUN/graph_state.json" \
  --initial-sql "SELECT AVG(revenue) FROM orders WHERE region = 'North'" \
  --config configs/cortex.yaml \
  --output workspace/online-human.json
```

`--initial-sql` is an optional independently generated candidate, which may
conflict with learned knowledge. It is not a reference answer. Omit it when no
candidate exists. A clear question may complete immediately; an ambiguous request
or conflicting candidate can return `status: needs_user_input`.

For `needs_user_input`, inspect `interaction_questions` and create
`workspace/answers.json` with the **actual IDs returned by this run**:

```json
[
  {
    "question_id": "COPY_ACTUAL_QUESTION_ID",
    "selected_option_ids": ["COPY_ACTUAL_OPTION_ID"],
    "explanation": ""
  }
]
```

Answer every question in the current round exactly once. Select one or more
returned option IDs. If no option fits, use `selected_option_ids: []` and provide
a nonempty `explanation` describing the desired interpretation.

Resume with the result JSON, which contains its `pending` state:

```bash
cortex online-context resume \
  --graph-state "$TRAIN_RUN/graph_state.json" \
  --pending workspace/online-human.json \
  --answers workspace/answers.json \
  --config configs/cortex.yaml \
  --output workspace/online-human.json
```

If another round is returned, replace the answers file using the new question
and option IDs and repeat `resume`. The result file may be overwritten as above.
Preserve the same graph, configuration, and checkpoint SQLite file throughout the
session. `resume` works across processes; changing the graph or deleting the
checkpoint requires a new `run`.

## 5. Online Context — setting B: Gold SQL simulates the user

For offline evaluation, add `--gold-sql` to the same public `run` interface:

```bash
cortex online-context run "Show the revenue for North." \
  --graph-state "$TRAIN_RUN/graph_state.json" \
  --initial-sql "SELECT AVG(revenue) FROM orders WHERE region = 'North'" \
  --gold-sql "SELECT SUM(revenue) FROM orders WHERE region = 'North'" \
  --config configs/cortex.yaml \
  --output workspace/online-simulated.json
```

The system first retrieves knowledge and produces its own clarification questions.
Only then does `SimulatedUserAgent` read Gold SQL, answer/review those questions,
and call the same `resume` interface used in setting A. This repeats until a
terminal result. Gold SQL is not supplied to retrieval, question organization, or
guidance synthesis. Do not copy Gold SQL into `--initial-sql` when evaluating.

The output adds `simulation.interaction_rounds` with the actual questions and
simulated answers. An empty array means no clarification was needed; it does not
prove that simulator answering was exercised. No Gold SQL is needed in setting A.

For a batch of JSONL cases, the existing evaluation script uses the same
Gold-only-to-simulator boundary:

```bash
python scripts/evaluate_online_context.py \
  --graph-state "$TRAIN_RUN/graph_state.json" \
  --cases data/dev.qa_sql.jsonl --dialect sqlite \
  --config configs/cortex.yaml --output workspace/online-batch.json
```

Each case must include `question` and reference `sql`; optional `initial_sql`
is a separate candidate. Inspect each record's `error` and
`online_after_interaction.status`, not only the script's exit code.

### Result contract and useful flags (both settings)

Terminal successful results contain:

```text
downstream_guidance.guidance_content
```

This is guidance text to insert into a downstream SQL-generation prompt. Online
Context itself does not generate or execute the final SQL.

| Status | Meaning |
| --- | --- |
| `needs_user_input` | Paused; answer current questions and resume (automatic in setting B) |
| `completed` | Terminal guidance with resolved choices |
| `completed_with_custom_guidance` | Terminal guidance incorporates a custom user explanation |
| `completed_with_unresolved` | Guidance exists but some choices remain unresolved, e.g. budget exhausted |
| `no_match` | No usable relevant knowledge; no terminal guidance |
| `unavailable` | Required context construction unavailable; no terminal guidance |

An Online Context command's exit 0 means a result was returned; inspect its
`status` and guidance before using it downstream. Exceptions exit nonzero.

| Flag | Purpose |
| --- | --- |
| `--question-budget N` | Override configured question budget; 0 does not mean all ambiguities are resolved |
| `--run-id ID` | Alphanumeric session ID; retry a failed `run` with the same ID and unchanged inputs |
| `--trace-output PATH` | Save organizer trace for the initial run |
| `--schema-context DIR` | Optional BIRD-style database column description directory |
| `--output PATH` | Save the result; also printed to stdout |

Use `cortex sql-train --help`, `cortex sql-extract --help`,
`cortex graph-merge --help`, and `cortex online-context run/resume --help`
(replace `run/resume` with the individual subcommand) for the complete flags.

## 6. Input format and Python APIs

Training accepts UTF-8 JSONL/NDJSON, a JSON array, or an object containing a
`training_set` or `queries` array. Use unique IDs and SQL valid for the configured
database:

```jsonl
{"source_id":"train-1","question":"What is total revenue in each region?","sql":"SELECT region, SUM(revenue) FROM orders GROUP BY region","evidence":null}
```

`question` and `sql` are required; `source_id` is recommended (generated when
omitted), and `evidence` is optional. Native `query` / `gt_sql` aliases are accepted.
Convert public BIRD/SPIDER datasets to this format with:

```bash
python scripts/prepare_public_inputs.py \
  --train /path/to/train.json --dev /path/to/dev.json \
  --database your_database_id --output data
```

Set `database.path` to your own database and keep all training examples within
that database. The adapter reads BIRD `SQL` and SPIDER `query` fields. No real
dataset is included in the repository.

The full offline Python interface is synchronous:

```python
import json
from pathlib import Path
from cortex.application import QASqlTrainingRecord
from cortex.bootstrap import build_sql_training_service
from cortex.schema import load_root_schema

pairs = tuple(QASqlTrainingRecord.model_validate(json.loads(line))
              for line in Path("data/train.qa_sql.jsonl").read_text().splitlines()
              if line.strip())
service = build_sql_training_service("configs/cortex.yaml", workspace=Path("workspace/api"))
result = service.run(training_set=pairs, schema=load_root_schema())
if result.status.value == "failed":
    raise RuntimeError("Inspect result.evaluation_report and result.diagnostics")
merged_state = result.state
Path("workspace/api/graph_state.json").write_text(merged_state.model_dump_json(indent=2))
```

Separate APIs are `build_sql_extraction` / `extract_training` and
`build_graph_merge_operator` / `merge_training_extraction`. Semantic merge needs
the extraction workspace's evidence resolver; the CLI wires it automatically.

Online Python APIs are asynchronous:

```python
import asyncio
from cortex.bootstrap import build_online_context_service, build_simulated_user_agent

async def online(merged_state, question, gold_sql=None):
    service = build_online_context_service("configs/cortex.yaml")
    current = await service.run(question=question, graph_state=merged_state, dialect="sqlite")
    if gold_sql is None:
        return current  # Present pending questions to the human, then call service.resume.
    simulator = build_simulated_user_agent("configs/cortex.yaml")
    prior_questions, prior_answers = [], []
    while current.pending is not None:
        questions = current.interaction_questions
        answers = await simulator.answer_round(
            question=question, gold_sql=gold_sql, dialect="sqlite",
            interaction_questions=questions,
            prior_questions=tuple(prior_questions), prior_answers=tuple(prior_answers),
        )
        prior_questions.extend(questions)
        prior_answers.extend(answers)
        current = await service.resume(
            graph_state=merged_state, pending=current.pending, answers=answers,
        )
    return current

# current = asyncio.run(online(merged_state, "What is total revenue for North?"))
```

Use `InteractionAnswer` objects for Python `service.resume`, and preserve the
returned `OnlineContextPending`. An async application should dispatch synchronous
training to a worker thread.

## 7. Troubleshooting

| Symptom | Check |
| --- | --- |
| Missing LLM configuration | Export `.env` in the current shell; verify the three required `CORTEX_LLM_*` values |
| 401/403 from provider | API key, endpoint, model access, and account permissions |
| 404 while validating/retrieving | Chat model name and endpoint; disable embeddings unless `/embeddings` is available |
| Tool/structured-output errors | Choose a model supporting tool calls/JSON; inspect case diagnostics |
| No such table / database file | `database.path`, config-relative paths, and that demo creation ran from the repo root |
| `needs_review` after training | Review case failures and merge conflicts; a graph export alone is not a passing run |
| Pending checkpoint missing/incompatible | Use the same config and graph; preserve checkpoint files or start a fresh session |
| Training/model timeout | Check provider connectivity and `llm.timeout`; allow sufficient model context and quota |

Tracing is optional: set `LANGSMITH_TRACING=true` with your own LangSmith key to
enable native traces. The workflows work with tracing disabled. Keep local input,
output, checkpoints, and credentials out of Git.

## 8. Verification record

On 2026-10-28, the SQLite workflow was checked with CPython 3.11, the pinned
runtime, a real configured chat endpoint, embeddings disabled, and tracing off:

- Fresh virtual environment: dependency installation and `pip check` passed.
- Synthetic `sql-train`: `completed`, 1/1 case passed, merged graph exported.
- Independent `sql-extract` followed by `graph-merge`: both `completed`.
- Human interface: real ambiguity → `needs_user_input` → answer submission in a
  separate CLI process → `completed` with nonempty guidance.
- Gold simulator: real clarification question → simulated answer → `completed`
  with nonempty guidance and one recorded interaction round.
- 677 existing local regression checks plus four release-specific checks passed,
  including multiple clarification rounds, Gold isolation, checkpoint paths,
  and optional provider extensions. Local tests and run artifacts are excluded
  from the published source.

These checks cover the documented paths and the synthetic data, rather than a
claim that arbitrary datasets, model outputs, or providers can never fail.
