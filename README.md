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
