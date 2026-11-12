# Interactive NL2SQL Agent

Implemented 2026-10-27. This is the interactive product workflow, independent of `baseline/` and its evaluation commands. The existing ontology build and graph explorer remain available.

## Start

```bash
scripts/start_ontology_web.sh /Users/yichendezaizai/CodeBase/DataMosaic 8012
```

Open `http://127.0.0.1:8012/ontomosaic-text2sql?view=ask&db=financial`.

The frontend lives in the existing DataMosaic portal, under `web/src/features/ontology/`. The API and agent live in this repository. The standard portal build includes the new Ask database tab.

Choose a database and an ontology from a prebuilt archive, a completed live build for that database, or an imported Graph JSON. Before a conversation is created, the ontology's physical table/column references are checked against the database. Each conversation gets a coherent SQLite backup and a validated ontology snapshot. This does not change the source database or shared ontology.

Model credentials use the existing `.env` and `configs/cortex.yaml`; `CORTEX_NL2SQL_CONFIG` can override the configuration path. The agent uses the configured default chat model; online context retains its existing model profile. Empty or literal `your-embedding-model` values use the existing lexical retrieval path, explicitly shown in the conversation. A configured embedding provider's errors remain visible and retryable.

## Runtime

New modules in `src/cortex/capabilities/nl2sql/`:

- `agent.py`: a JSON-action tool loop for schema/value inspection, SQL proposals after ontology-based clarification; final SQL meaning review and result summarization.
- `database.py`: identifier-validated inspection, SQLGlot validation and EXPLAIN, read-only SQLite execution with authorizer, 8-second deadline and 200-row preview.
- `service.py`: persisted outer workflow and the adapter to the existing public online-context run/resume service.
- `store.py`: atomic SQLite task/event writes, task versions and replayable event sequence numbers.
- `grounding.py`: scope-aware SQL AST field links to actual ontology IDs, UTF-16 editor positions and separately labelled agent declarations.

`src/cortex/web/nl2sql.py` supplies the API, mounted by `web/app.py`. `create_app(query_runtime_factory=...)` allows deterministic agent/model dependencies in integration tests without replacing the real online workflow.

Persistent state is under `CORTEX_WEB_STORAGE/nl2sql/` (default `workspace/web/nl2sql/`): `sessions.sqlite`, and one directory per conversation containing `database.sqlite`, `graph.json`, and `online.sqlite` checkpoints. Run one API worker, consistent with the existing workbench. This is local durable background scheduling, not a distributed queue.

The outer steps for new tasks are prepare → online context (with the original NLQ and no assumed initial SQL) → graph-backed user clarification → final generation → validate/review/execute → summarize. The SQL agent cannot initiate ungrounded clarification before graph retrieval. Real validation/execution/review errors feed back into generation, with up to three SQL attempts and 24 agent actions per task. Every failed SQL revision remains visible. Database execution is read-only, so an interrupted query can safely be executed again before its result was saved.

## Clarification and recovery

- The service presents the exact questions and option IDs returned by online context. Multiple selections and free-text explanations are supported.
- One complete question round must be answered before `resume` is called. No simulated user is used.
- `awaiting_user` releases the worker; the UI can close or refresh. Pending state is loaded server-side, never accepted from the browser.
- The exact original graph snapshot and persisted online run ID are used to resume. The existing workflow rejects incompatible prompts/configuration and stale pending state.
- Answers are committed with an interaction ID, expected task version and idempotency key. Unknown/duplicate/missing choices are rejected. Replayed identical submissions return the current task without consuming them again.
- `completed_with_unresolved` produces `needs_review`; `no_match` is explicitly shown and permits schema-based generation; `unavailable` produces a retryable failure.
- Agent-owned clarifications use the same UI and durable answer protocol. Their confirmed details are included when starting online context. Conversation history is supplied to resolve follow-up references.
- Process shutdown/restart marks executing tasks interrupted. Resume continues from their saved outer step and inner checkpoint. Cancellation is persisted. Editing meaning creates a new task revision and retains the original result.

Online context has an optional `on_retrieved` observer, added without changing retrieval/clarification semantics or workflow fingerprints. It lets the UI see the real retrieved graph before question organization finishes. Subsequent rounds show selected answers; the final processed graph and `projection_trace` appear after online finalization.

## API

All routes below are under `/api/nl2sql`; `/ontology/api/nl2sql` and `/ontomosaic-text2sql/api/nl2sql` use the existing same-origin middleware.

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/sources?database_id=...` | Available ontology sources |
| POST / GET | `/sessions` | Create/list conversations |
| GET | `/sessions/{id}` | Conversation with task snapshots |
| POST | `/sessions/{id}/tasks` | NLQ/follow-up/revision; returns 202 |
| GET | `/tasks/{id}` | Full public task snapshot |
| GET | `/tasks/{id}/activity` | Saved event history |
| GET | `/tasks/{id}/events?after=...` | SSE with sequence IDs and Last-Event-ID replay |
| POST | `/tasks/{id}/answers` | Answer a complete active interaction |
| POST | `/tasks/{id}/retry` | Resume failed/interrupted work |
| POST | `/tasks/{id}/cancel` | Stop the task |
| GET | `/tasks/{id}/artifacts/{artifact}` | Retrieved/processed graph, SQL revision or result |

The browser uses SSE plus snapshot polling as a fallback for buffered proxies. Draft answers are saved locally per interaction so a refresh does not erase selections. Public snapshots omit internal pending state, raw model context, schemas and observations.

## Graph/SQL links and limits

The existing canvas graph controller now exposes node selection, highlighting and focus, with instance-scoped controls. Conflicts are read from current inline node alternatives, while older envelopes remain supported.

The evidence panel shows actual retrieved/processed graphs, guidance, SQL revisions and real query results. Clicking a clarification option's evidence reference finds its node/target/alternative owner. SQL identifiers are clickable, highlighting related graph nodes; selecting a node lists its SQL references. Node definitions and provenance are expandable.

AST matching resolves physical columns within query scopes and matches their declared graph dependencies. It is labelled field-reference matching, not proof that a complete business rule was obeyed. Agent usage declarations are labelled separately. Derived-table lineage and complex rule-to-expression proofs are not implemented; unsupported references are left unlinked rather than guessed. SQL meaning review is model-assisted and does not guarantee semantic correctness.

## Verification

```bash
.venv/bin/python -m pytest tests/test_nl2sql_agent.py tests/test_web_workbench.py tests/test_web_database_upload.py tests/test_portal_integration.py tests/test_retrieval_online_handoff.py tests/test_online_workflow_review.py -q
npm --prefix /Users/yichendezaizai/CodeBase/DataMosaic/web run build
```

Integration tests use the real online-context retrieval/projection/SQLite checkpoint code, injected deterministic LLM decisions, and an actual test database. They cover restart/answer recovery, repeated/stale submissions, multiple rounds, custom explanations, query repair, business-meaning review, empty results, cancellation, interrupted outer-agent recovery, SQL write/function guards, deadlines, row limits and AST positions.

A live model smoke run on Financial completed with a user choice made through the actual page: 73 retrieved/processed nodes, 103 edges, five SQL field bindings, and 88 gold cards with OWNER disposition. The result is from SQLite, not the design mock's simulated numbers. Task `9b1d77f85d194713a9765affc5a55913` is saved in the local web runtime; its initial embedding-placeholder failure was resumed after enabling the explicit lexical path.

A second live follow-up in the same conversation completed with `COUNT(DISTINCT d.client_id)`, 100 nodes / 149 edges, five SQL field bindings, and model-assisted meaning review marked consistent. It returned 88 distinct gold-card owner clients. Both actual executions are recorded locally in `workspace/nl2sql-live-verification.json`. The final related backend verification passed 52 tests, and the portal production build passed.

## Demonstration correction

The earlier smoke question containing “请先确认…” and its follow-up were manually scripted tests, not evidence that ontology discovered those choices. That smoke conversation is soft-archived, with its SQL and results retained. The new demonstration uses one natural NLQ without an instruction to ask questions; pending options come only from online context and carry actual graph refs. The conversation shows the current main question, its graph clarification and answer together; older main questions remain collapsed. A `session` URL parameter can select the clean demonstration explicitly. No question is fabricated if the retrieved graph does not establish competing interpretations.

The graph-first path exposed a dependency-closure bug: table metadata may reference columns by local names (primary/foreign keys and time columns). Closure now resolves those names in the owning table, matching graph validation, before retaining dependencies. Completed retrieval checkpoints reuse their saved recall/relevance judgments and deterministically reassemble closure on replay, allowing recovery from the earlier incomplete projection without repeating model selection. Regression tests cover this replay and verify that a natural NLQ reaches online context unchanged, before the SQL agent is invoked.
