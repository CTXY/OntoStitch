# OntoStitch for NL2SQL

OntoStitch learns reusable ontology knowledge from natural-language question–SQL
pairs and uses it to guide SQL generation. It extracts concepts, metrics, and
generation rules, merges them with database schema knowledge into an ontology
graph, and retrieves relevant knowledge for new questions.

## Installation

Use **Python 3.11** on Linux or macOS (or WSL). Run the following commands from
Bash/Zsh:

```bash
git clone https://github.com/CTXY/OntoStitch.git
cd OntoStitch
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt -e .
cp .env.example .env
```

Set your OpenAI-compatible chat model connection in `.env`. The model must support
tool calling and structured JSON output:

```dotenv
CORTEX_LLM_MODEL=your-chat-model
CORTEX_LLM_BASE_URL=https://api.openai.com/v1
CORTEX_LLM_API_KEY=your-api-key
```

Export these settings before running commands in each new shell:

```bash
set -a
source .env
set +a
```

Embeddings are optional. Leave `CORTEX_EMBEDDING_MODEL` unset, or set it only if
your endpoint supports the chosen embedding model. Keep `.env` private.

## 1. Prepare data

Provide a SQLite database and a UTF-8 JSONL training file, with one record per
line. All records in one training run must refer to the same database.

```jsonl
{"source_id":"train-1","question":"What is total revenue in each region?","sql":"SELECT region, SUM(revenue) FROM orders GROUP BY region","evidence":null}
```

- `source_id`: unique record ID.
- `question`: natural-language question.
- `sql`: reference SQL that runs against your database.
- `evidence`: optional context explaining the question.

Save the training file as `data/train.qa_sql.jsonl`. Set the database path in
`configs/cortex.yaml`:

```yaml
database:
  type: sqlite
  path: data/databases/demo/demo.sqlite
```

Paths in this configuration resolve against the repository root. The supplied
configuration already uses SQLite for SQL validation.

For BIRD/SPIDER datasets, filter one database and normalize its records with:

```bash
python scripts/prepare_public_inputs.py \
  --train /path/to/train.json --dev /path/to/dev.json \
  --database your_database_id --output data
```

This creates `data/train.qa_sql.jsonl` and `data/dev.qa_sql.jsonl`; set
`database.path` to the corresponding SQLite file.

**Try the synthetic example:** instead of preparing your own data, run the
following command. It creates a three-row orders database and matching input
files at the default paths used throughout this README:

```bash
python scripts/create_demo.py
```

## 2. Generate the ontology graph

Run extraction, SQL validation, and graph merge together:

```bash
cortex sql-train --config configs/cortex.yaml \
  --training-set data/train.qa_sql.jsonl \
  --output workspace/train \
  --result-file workspace/train-summary.json
```

The command creates a run directory and exports the merged ontology as
`graph_state.json`, plus `graph.html` for inspection. Read the run path from the
summary for use in the next step:

```bash
TRAIN_RUN="$(python -c 'import json; print(json.load(open("workspace/train-summary.json"))["output"])')"
```

A `completed` result (exit 0) means the run succeeded. `needs_review` (exit 2)
exports a graph with items to inspect in `result.json`; `failed` (exit 1) does not
publish a new final graph.

## 3. Generate guidance

Online Context takes the merged graph and a new question. It returns
`downstream_guidance.guidance_content` for a downstream SQL generator.

### Human interaction

```bash
cortex online-context run "Show the revenue for North." \
  --graph-state "$TRAIN_RUN/graph_state.json" \
  --config configs/cortex.yaml --output workspace/online.json
```

If the result has `status: needs_user_input`, read `interaction_questions` and
create `workspace/answers.json` using the returned question and option IDs:

```json
[
  {
    "question_id": "COPY_RETURNED_QUESTION_ID",
    "selected_option_ids": ["COPY_RETURNED_OPTION_ID"],
    "explanation": ""
  }
]
```

Answer every question in the current round. If no option fits, use an empty
`selected_option_ids` list and provide your interpretation in `explanation`.
Then resume:

```bash
cortex online-context resume \
  --graph-state "$TRAIN_RUN/graph_state.json" \
  --pending workspace/online.json --answers workspace/answers.json \
  --config configs/cortex.yaml --output workspace/online.json
```

Repeat for any further clarification rounds. Keep the same graph, configuration,
and local checkpoint files. A question without ambiguity may return guidance
immediately.

### Gold SQL simulated interaction

For offline evaluation, provide reference SQL with `--gold-sql`. The simulator
answers clarification questions and resumes automatically:

```bash
cortex online-context run "Show the revenue for North." \
  --graph-state "$TRAIN_RUN/graph_state.json" \
  --gold-sql "SELECT SUM(revenue) FROM orders WHERE region = 'North'" \
  --config configs/cortex.yaml --output workspace/online-simulated.json
```

Gold SQL is supplied only to the simulated user after clarification questions
exist. The result includes `simulation.interaction_rounds` for inspecting its
questions and answers.

Both modes accept optional `--initial-sql "..."` for an independently generated
candidate SQL and `--question-budget N` to limit clarification questions. Do not
use Gold SQL as the initial candidate when evaluating.

Read `downstream_guidance.guidance_content` from the terminal result JSON.
`completed_with_unresolved` means some choices remain unresolved; `no_match` or
`unavailable` means no guidance was produced. Online Context produces guidance;
your downstream SQL generator uses it to generate the final SQL.

## Standalone database metadata (M0)

`cortex metadata` initializes metadata **before** case extraction. It accepts a
SQLite database file and optional initial description files. It does not read
training cases, modify the source database, or run ontology/global merge.

Generation has three independent stages:

1. Database DDL and profiling, performed by code.
2. File import, with exactly two settings: dataset-specific loader or autonomous
   file Agent. A loader owns any dataset-specific processing it needs.
3. Optional database exploration and semantic supplementation, enabled by --agent.

The BIRD loader parses CSV files and resolves columns by code: exact names first,
then a unique match after trimming surrounding whitespace; ambiguous matches are
reported rather than guessed. It internally calls
the supplied model to interpret nonempty column_description/value_description text
together, in batches of up to 20 columns (40,000 input characters). The only output
properties are description and value_description. This is faithful extraction:
source labels may be preserved or left null, never expanded using column names or
common knowledge. Label-only sources also have an explicit no-expansion check.
There is no separate file mode
or normalization switch. The interpreter has no database tools. Output must cover
exactly the input targets; invalid or ambiguous records are left unchanged and
reported. Original files remain available. Stage 3 reads the imported result.

Database enrichment currently uses one Agent conversation and budget for the
database (60 model calls and 240 tool calls by default). It must inspect every
column, but uncertain descriptions may remain null. Batch reads reduce tool
overhead; unread columns are recorded in the coverage trace and prevent a
completed status. It discovers relevant objects using paginated directories, inspects
them individually, and may submit atomic batches of semantic edits.

```bash
# DDL + profiling + BIRD loader (uses the model configured in configs/cortex.yaml).
cortex metadata build --database data/shop.sqlite --workspace workspace/shop-metadata \
  --initial-file data/database_description --loader bird --profile-rows 10000

# Unknown file layout: an Agent discovers its structure and imports metadata.
cortex metadata build --database data/shop.sqlite --workspace workspace/shop-agent \
  --initial-file data/notes.json --file-mode agent --config configs/cortex.yaml \
  --context 'Retail orders; inspect line versus order granularity.'

cortex metadata show --workspace workspace/shop-metadata --object-id table.orders
cortex metadata export --workspace workspace/shop-metadata --output m0.json
cortex metadata schema --kind column > column-schema.json
cortex metadata list --workspace workspace/shop-metadata --kind column --table-id table.orders
```

Metadata schema 3.3 stores tables and columns by their original names, without
redundant IDs. Declared foreign keys also have no ID. Primary/unique keys use local column names; foreign keys contain
the target table name and target column names. Relation source_columns/target_columns use table.column
names without the column. prefix or percent encoding; components containing dots
or double quotes are quoted to keep references unambiguous. Tool directories still return
derived addresses (such as `column.orders.amount`) for unambiguous updates; these
are not stored object properties. Existing snapshot versions migrate on read.

Omit `--initial-file` when none are available; repeat it for multiple files or
directories. Text inputs support CSV, JSON/JSONL, YAML, Markdown and TXT (up to
2 MB per file). File import has two settings:

- --file-mode loader (default): use a dataset-specific loader.
  - --loader bird: code resolves CSV rows to columns; the loader internally uses
    LLM batches to convert the two raw description fields into our two metadata
    fields. Blank rows do not call the model. CLI loads configs/cortex.yaml by
    default; Python callers supply model when loading nonempty descriptions.
  - --loader normalized: directly import already-normalized JSON using code.
    Only an omitted column means table-level metadata; null/empty column names
    are invalid. Custom loaders remain supported.
- --file-mode agent: an Agent autonomously reads unknown file structures, locates
  database objects and writes supported metadata. No dataset loader runs.

--agent independently enables stage 3. Selecting the BIRD loader does not enable
database exploration. Its model-call budget applies per input CSV; autonomous file
interpretation and stage-3 enrichment each have their own run budget.

```json
{
  "entries": [
    {"table": "orders", "metadata": {"description": "Current order records"}},
    {"table": "orders", "column": "amount", "metadata": {
      "representation": "Integer amount stored in CNY cents."
    }}
  ]
}
```

For another dataset, subclass `MetadataLoader` in
`capabilities/metadata/loading.py`, implementing
`extract(snapshot, source) -> LoaderResult(batches, supported, issues)`. The loader interprets
one source without mutating the snapshot. The service applies changes through
`apply_changes`, owns conflict checks, and adds the file ID to object-level `source_refs`.
Expose a no-argument factory through `--loader my_package.shop_import:make_loader`,
or pass `loader=MyLoader()` to `build_metadata_service`. Custom loader code is
explicit trusted Python. Each batch is one source record that must succeed or
fail atomically. Normal imports validate once per file; conflicts fall back to
record-level validation. Unsupported formats and invalid records produce issues;
an empty supported file is a normal no-op. Legacy `adapter=` / `--adapter` and
list-of-change returns have been removed; use the single loader contract. Raw text-file collection is separate from interpretation
in `infrastructure/metadata/files.py`: `read_initial_files()` returns source records
directly, without a collection wrapper. `ports.py` contains only the database-source
and snapshot-repository protocols; the service uses `MetadataAgent` directly.

Python callers select unknown-file interpretation with
`build_metadata_service(workspace, model=model, file_mode="agent")`.
Database enrichment requires explicit `enrich_database=True` and a model; simply
passing a model does not enable an extra pass.
Agent-derived interpretations cite the original file in object-level `source_refs`. Dataset loaders and the file Agent are alternatives, not an implicit fallback.

The optional Agent chooses which supplied files, objects and SQL probes to inspect
and which semantic fields to fill. Its tools expose paginated files, JSON schema,
individual objects, read-only SQL, semantic edits and data-relation proposals.
`list_metadata` returns paginated object summaries; `inspect_metadata` looks up
one object directly and returns its typed view. Table views include a paginated
column directory, while column views contain complete column metadata.
`metadata_schema(kind)` generates field descriptions directly from Pydantic.
All writable properties are plain strings or null, with one simple `MetadataChange`
contract. Each edit replaces a string with an expected-value check; nested semantic
objects are rejected.
Python clients share these views through `service.list_metadata/inspect/schema`.
Use `cortex metadata sources` to browse sources, adding `--source-ref ID` to
read their contents. The Markdown Agent prompt lives at
`src/cortex/capabilities/metadata/prompts/enrich.md`; file interpretation uses
`prompts/files.md`.
`--max-agent-calls` and `--query-timeout` bound its work. The CLI saves tool/model
traces as `agent-trace-r<N>.json`, `file-agent-trace-r<N>.json`. Reports contain
status, publication outcome and trace. Failed builds retain attempt traces named
`*-trace-failed-<attempt>.json`. Python clients can inspect `service.runs`, whose
AgentResult values contain snapshot/status/trace/error. Model/network failures fail the build rather
than pretending enrichment succeeded; budget exhaustion records incomplete
coverage in `issues`. Invalid tool calls and truncated responses also mark the
run incomplete rather than silently reporting normal completion.

The fixed contract is defined in `capabilities/metadata/models.py`:

| Object | Content |
| --- | --- |
| Database | Source identity, SQLite read snapshot, optional domain description |
| Table | Declared keys, description, grain and scope text, columns, profile, source_refs |
| Column | Type/default/nullability, description, value_description and representation text, profile, source_refs |
| Relation | Provisional join, functional dependency, or existing-data derivation |
| Source registry | Source IDs and locations; original text and probe results are read separately |

All authored semantics are flat text. Tables have `description` (purpose), `grain`
(what one row represents) and `scope` (inclusion and temporal organization).
Columns have `description` (meaning and repeated measures),
`value_description` (codes, NULL/sentinel meanings and conditions),
and `representation` (stored format, units and timezone in
plain language). Database/relation objects have a description. Unknown fields stay
null. No nested value-domain, value-grain or representation objects are required.
`schema_name`, table-level `ddl` and unused `grain_duplicate_groups` were removed;
original table/view/index DDL remains in the schema source, read on demand.
JSON Schema rejects extra fields and validates object/source references. IDs encode
exact names and are scoped to one database; do not reconstruct them from display
names. Integration with the training graph still needs explicit ID mapping.

Profiles live **inside** Table/Column objects. At most `--profile-rows + 1` rows
are fetched to distinguish a full scan from a prefix sample. Sample statistics
are never extrapolated; `row_count` stays null for incomplete scans. `min/max` are
finite numeric observed extrema, not allowed bounds; text/mixed/empty columns omit them. Profiles include time,
scope, sampling method, rows examined and an opaque read-transaction snapshot ID
(not a content checksum). Empty columns have unknown null fraction. Primary-key
nullability stays unknown where declaration details have not established it.
Partial/expression uniqueness is not promoted to an ordinary key; original index
DDL is retained in the schema source. Foreign-key references resolve SQLite's
ASCII-insensitive identifiers to exact table/column names. Probe results with duplicate
column labels are rejected explicitly: supply unique SQL aliases instead of
silently losing values.

Each object has a simple `source_refs` list; there is no `provenance`, origin or
per-property annotation structure. Edit source references are deduplicated and
accumulated as consulted sources, not proof of current correctness. Optional edit
reasons stay in traces. Raw source contents are kept internally and paginated by
`read_source`; exports include headers only. Schema 1.0/1.1 snapshots migrate on read
to 2.0: old nested explanations become text, old provenance becomes object-level
source references, and original DDL moves to schema sources. Stored revisions are
not rewritten. New normalized imports must use strings, not legacy nested objects.

`workspace/metadata.sqlite` is the standalone M0 authority for this phase. It
stores immutable revisions with transactional expected-revision checks; exported
JSON is a read projection. Rebuilding requires explicit `--expected-revision N`
and creates a revision from source inputs (it does not merge previous edits).
Do not point this repository at the Web app's unrelated `metadata.sqlite`.
Training-graph integration, case repair, assessment and global reconciliation
are deferred.

Python API:

```python
from cortex.bootstrap.metadata import build_metadata_service
from cortex.capabilities.metadata.models import BuildOptions
from cortex.capabilities.metadata.management import MetadataChange

service = build_metadata_service("workspace/shop-metadata")
m0 = service.build("data/shop.sqlite", options=BuildOptions(profile_rows=5000))
table = service.inspect("table.orders")
new_revision = service.update([
    MetadataChange(object_id="table.orders", field="description", value="Order records",
                   expected_value=table.description, reason="Provisional interpretation")
], expected_revision=m0.revision)
```

Use `service.load_snapshot(revision)` for a complete internal snapshot and
`service.inspect(object_id)` for object views; the old `read()` API was removed.
Repository construction is side-effect free; source/repository path collisions
(including symlinks and hardlinks) are rejected before initialization. BIRD CSV
parsing is shared with the existing online/training consumers through
`infrastructure/bird_descriptions.py`. Historical snapshot migration remains supported.

Layers: `capabilities/metadata` owns models/edit contracts/Agent behavior;
`infrastructure/metadata` owns SQLite access, file adapters and persistence;
`application/metadata.py` orchestrates initialization and updates;
`bootstrap/metadata.py` composes adapters; `cli/metadata.py` exposes commands.
Replace the source adapter to add another database engine. The first shipped
implementation supports SQLite files.
