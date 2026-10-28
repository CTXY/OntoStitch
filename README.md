# OntoStitch for NL2SQL

> Source-only release: datasets, example data, experiment outputs, local workspaces, ablation studies, and tests are not distributed. Supply your own local data and configuration. Demo commands that reference these files require local preparation.

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

Extraction now uses one Learning Agent (`LangChain create_agent`) for initial
learning and repair, with six domain tools and short working notes. Independent
Diagnosis and Assessment provide feedback; one case-level LangGraph owns validation,
acceptance, recovery and output with a persistent SQLite checkpointer. All configured Tables/Columns are initialized;
Values are included conditionally after database verification.

Training and Graph Merge expose synchronous Python APIs: call
`SqlTrainingService.run(...)`, `SqlTrainingCapability.train(...)`, or
`GraphMergeOperator.merge(...)` directly, without `await` or `asyncio.run`.
Case workers and parallel read-only agent tools retain their existing concurrency.

### Graph Merge checkpoint and agents

Graph Merge uses one LangGraph consolidation loop: prepare → Consolidator →
Assessment → finalize. The two roles use LangChain `create_agent` and share two
read-only tools, `read_evidence` and `read_nodes`. Physical, unchanged existing,
single-definition and identical-definition groups use deterministic shortcuts.
Conflicts retain complete alternatives; failed revision preserves original content.
Each group receives its own source evidence. Candidate formulas, nested references
and SQL plan structure are checked before assessment. Changed definitions and their
complete alternatives synchronize compiled dependency edges; unrelated groups and
other semantic relationships are preserved.
There is no miner, legacy reconciliation backend, Store, or cross-run agent memory.

Graph Merge uses the official `SqliteSaver` with durable, file-backed SQLite checkpoints.
No database server is required. The default is `workspace/checkpoints/graph_merge.sqlite`;
relative paths resolve against the configuration directory (repository root for `configs/`).
Optionally override it:

```bash
export GRAPH_MERGE_CHECKPOINT_URL='sqlite:///workspace/checkpoints/graph_merge.sqlite'
```

Checkpoint tables are initialized automatically; each merge owns and closes its
connection. PostgreSQL and in-memory checkpoint URLs are rejected. Tests can inject
`InMemorySaver` explicitly. Web training uses a separate SQLite checkpoint file
for each run, alongside its input snapshot, progress events and generated graph.

For separate pre-merge output, `scripts/merge_training_output.py` prints and saves
`merge_run_id.txt` before execution. Resume by repeating the command with the same
inputs/output and `--run-id ID`. Python callers use `operator.merge(existing,
incoming, schema, run_id=ID)`. Completed runs return the saved result. Changed
inputs, configuration or rendered prompts are rejected for that run ID; a new
run uses a new ID. Identity groups and source evidence are checkpointed before
group processing. Official child checkpoints retain the working state; a model
call interrupted before its result was saved can run again. Formal graph saving
remains at the application boundary.

Set `LANGSMITH_TRACING=true` and `LANGSMITH_API_KEY` to enable native model and tool
spans, token usage, and cost estimates where model pricing is available. Remote
inputs/outputs and exception payloads are filtered; full merge evidence stays in
local artifacts/SQLite checkpoints. Custom model names may need LangSmith pricing
configuration. Checkpointing and cost observation do not require a billing ledger.

Code is organized as follows:

```text
capabilities/graph_merge/
  operator.py                  graph assembly and canonical mappings
  identity.py / edges.py       matching and deterministic graph operations
  reconciliation/             proposal models, validation, materialization, facade
  workflow/                   run checkpoints, group state and four-node loop
  llm/                        agents, read tools, reference codec
    prompts/                  role registry, shared rules, type-specific fragments
infrastructure/workflow/      official SQLite saver and native LangSmith tracing
```

Prompt composition and maintenance are documented in
`src/cortex/capabilities/graph_merge/llm/prompts/README.md`. Prompts are versioned
with source code and identified by their rendered hashes, rather than fetched
from a mutable remote "latest" version during a run.
Online retrieval/context APIs remain asynchronous; training adapts RetrieveDB at
its dedicated retrieval boundary. An asynchronous host should dispatch the
synchronous training service to a worker thread.

Ontology revisions and detailed evidence remain independently stored. The former
manual controller and per-type extraction/optimization runtimes are removed.

Each execution gate requires three consecutive correct SQL regenerations by
default and stops at the first failure. Working changes dynamically; accepted
improvements advance a separate retained snapshot. Finalization discards tentative
changes and verifies the actual retained graph, reusing exact matching evidence
within the same case. Only `ready` semantic fragments enter normal Graph Merge.

See the [case state machine](docs/design/extract-case-agent-loop.md),
[implementation design](docs/design/extract-implementation.md), and
[prompt maintenance guide](src/cortex/capabilities/sql_training/llm/prompts/README.md).
Case directories under `train_runs/<run>/cases/` contain complete snapshots,
evidence, a safe checkpoint and the final semantic bundle. The programmatic
`TrainPipeline.resume_case(case_root)` resumes one case using the same configuration,
source and fixed database; completed bundles can be read without rerunning learning.

Set standard `LANGSMITH_TRACING=true` and LangSmith connection variables to enable
native LangGraph/agent/model/tool traces, readable inputs/outputs and token usage.
Credential values are redacted before upload; ontology snapshots and evidence remain
the local recovery source. Custom model costs require matching LangSmith pricing. Without
tracing configuration the pipeline runs locally.

### Separate training and graph merge

The two offline modules have public builders and synchronous Python APIs:

```python
from cortex.bootstrap import build_sql_extraction, build_graph_merge_operator
from cortex.application import extract_training, merge_training_extraction

engine = build_sql_extraction("configs/cortex.yaml", workspace="workspace/extract")
extraction = extract_training(engine, training_pairs, schema, existing_state)
# extraction.state is an incoming fragment, not a complete merged graph.
operator = build_graph_merge_operator("configs/cortex.yaml", evidence_resolver=evidence_resolver)
result = merge_training_extraction(extraction, operator, schema)
merged_graph = result.state.graph
```

The bootstrap layer has two public training builders:

| Builder | Returns | Scope |
| --- | --- | --- |
| `build_sql_extraction(...)` | `SqlTrainingCapability` | Standalone extraction |
| `build_sql_training_service(...)` | `SqlTrainingService` | Extraction, merge and graph publication |

Both accept a config file path or an already loaded `CortexConfig`. The service
builder delegates extraction construction to the engine builder; there is no
separate private service builder. Constructing dependencies belongs to bootstrap;
`extract_training(...)`, `merge_training_extraction(...)` and `service.run(...)`
execute the corresponding application workflows.

Passing `engine=` reuses that extraction dependency and preserves the default
semantic merge behavior. Supply the matching `workspace=` so merge reads its
source evidence. For deterministic merging without model calls, explicitly pass
`graph_merge=build_graph_merge_operator(config, live_semantics=False)`.

`build_sql_extraction` builds extraction resources only, without merge model,
embedding or graph repository initialization. The lower-level
`engine.train(training_pairs, schema, existing_graph)` and
`operator.merge(existing_state, incoming_state, schema, run_id=...)` remain available.
Use `extract_training` and `merge_training_extraction` for the shared training gates,
terminal reports and optional repository publication. For semantic merge, supply
source evidence; the CLI wires the training workspace evidence resolver automatically.
Both the full CLI and separate merge use the configured `graph_merge` model profile.

The independent commands are:

```bash
cortex sql-extract --config configs/cortex.yaml \
  --training-set data/train.qa_sql.jsonl --output workspace/premerge

cortex graph-merge \
  --training-workspace workspace/premerge/<run> \
  --config configs/cortex.yaml --output workspace/merged
```

`sql-extract` accepts the same JSON/JSONL/NDJSON records, wrappers and field aliases
as `sql-train`. It creates a dated run directory. Its formal handoff is
`extraction_result.json`, containing the incoming state, complete evaluation report,
base graph and schema hash. `incoming_state.json`, `incoming_graph.json` and
`evaluation_report.json` are inspection views. A graph alone cannot authorize merge;
old workspaces without the complete handoff must be regenerated with `sql-extract`.

| Parameter | Commands | Meaning |
| --- | --- | --- |
| `--training-set PATH` | `sql-extract` | Required training pairs, same format as `sql-train` |
| `--training-workspace DIR` | `graph-merge` | Required extraction run with `extraction_result.json` and evidence artifacts |
| `--output DIR` | Both | Required; extraction creates a dated child, merge uses this exact directory |
| `--config PATH` | Both | Defaults to `configs/cortex.yaml` |
| `--schema PATH` | Both | Optional root schema override; merge must match extraction |
| `--existing-state FILE` | Both | Exported base `GraphState` JSON; merge checks it matches the extraction base |
| `--graph-state DIR` | Both | Persistent graph bundle; extraction only reads, merge validates and updates atomically |
| `--run-id ID` | `graph-merge`, `sql-train` | Stable merge ID for checkpoint recovery |
| `--progress-file PATH`, `--result-file PATH` | All three | Optional progress events and terminal summary |

`--existing-state` and `--graph-state` are mutually exclusive. Without either flag,
extraction starts with an empty base; merge uses the base saved in the handoff.
To update an existing graph, pass the same `--graph-state DIR` to extraction and merge.
To use an exported JSON graph, pass `--existing-state FILE` when extracting;
merge retains that base automatically. A different base requires new extraction,
because incoming fragments may reference existing nodes. An existing merge output
directory requires `--run-id` matching its `merge_run_id.txt`.

The previous script entry points remain aliases with the same flags and exit codes:
`python scripts/run_sql_training_premerge.py` runs `sql-extract`, and
`python scripts/merge_training_output.py` runs `graph-merge`.

Full and split training share one terminal contract:

- `completed` / exit 0: extraction and merge finished without review items.
- `needs_review` / exit 2: nonfatal case failures, extraction errors, unresolved
  graph conflicts or merge diagnostics requiring review. Valid `ready` fragments
  are still merged and the validated final graph is exported.
- `failed` / exit 1: fatal extraction (including a fatal case), invalid handoff,
  merge initialization/execution failure, final graph validation or publication failure.
  Fatal extraction never initializes or invokes merge. Failed integration does not
  publish a candidate graph.

Deterministic cleanup diagnostics `STALE_REFERENCE_EDGE_DROPPED` and
`INVALIDATED_RELATIONSHIP_DROPPED` are informational; all other current merge
codes require review. Existing unresolved node conflicts also require review even
when merge emits no diagnostic.

`sql-train` and `graph-merge` export the same `result.json` structure. Its
`merge_report` preserves the run ID, complete node/edge mappings, diagnostics and
candidate state, and is also exported as `merge_report.json` when available.
Successful/review results have `state == merge_report.state`; failures retain the
base state and may retain a rejected candidate report for inspection. Runtime
failures record `failure_stage` and `failure_message` in `diagnostics.context`.
The final `graph.json`, `graph_state.json` and `graph.html` are exported only for
`completed` or `needs_review` results.

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


## Ontology web workbench

The integrated DocSensaAI frontend now supports preloaded SQLite previews,
question–SQL JSONL import, real `cortex sql-train` execution, and an embedded
ontology explorer with PNG/SVG export and Graph JSON import/save.
The build button trains the first one or two imported cases, reports actual case
and merge progress, and loads that invocation's generated graph. Task metadata
and official LangGraph checkpoints use file-backed SQLite; no PostgreSQL server
is needed. Archived graphs remain available through their separate explorer.
See
[setup and implementation](docs/design/ontology-web-implementation.md).

### Interactive database question answering

The ontology workbench now includes **Ask database**: a dedicated NL2SQL agent
with real online-context retrieval, user clarification, persistent pause/resume,
SQL validation and read-only execution, and graph/SQL evidence links. It does not
use or modify the experimental `baseline/` workflow.

Start the existing portal and API with
`scripts/start_ontology_web.sh /path/to/DataMosaic 8012`, then open
`http://127.0.0.1:8012/ontomosaic-text2sql?view=ask&db=financial`.
Select the database and an ontology source, ask a question, and answer any
clarification cards. Conversations and checkpoints survive page refreshes and
API restarts. See [implementation and verification](docs/design/nl2sql-agent-implementation.md)
for configuration, API contracts, execution limits and linkage semantics.
