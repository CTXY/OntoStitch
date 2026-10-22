# Extraction prompt management

`load_prompt(name)` is the single registry. Keep instructions in Markdown and
domain contracts in `cortex/schema/ontology_asset_definitions.py`; do not copy type
schemas into multiple role prompts. `prompt_fingerprint(name)` hashes the composed
instructions, including shared extraction rules. Restart a running process after
editing prompts; a run uses a frozen cached prompt version.

## Default agent pipeline

| Name | File | Responsibility |
| --- | --- | --- |
| learning_system | learning/system.md | Role, authority, tools, context, notes and handoff |
| learning_direct | learning/direct.md | Joint initial extraction, type coverage and completion |
| learning_repair | learning/repair.md | Fixed targets, autonomous investigation and coherent graph edits |
| learning_assessment | learning/assessment.md | Independent evidence-based group verdicts and dependencies |

The Learning system prompt composes the existing initial domain rules while
excluding the old one-shot response envelope. Tool descriptions specify arguments
and effects. `inspect_graph(mode="guide", node_type=...)` returns the exact compact
Pydantic property schema plus a reference to the complete contract. Detailed rules
and Rule taxonomy remain available on demand.

Dynamic source, graph and feedback data are delimited from instructions. Default
context contains semantic overviews, not every physical node or every previous
round. Long tool results become case evidence references, retaining tool-call/result
pairs. Assessment receives current semantic content, changed prior definitions and
compact per-attempt evidence, without Learning's private conversation.

## Changing prompts

1. Change the relevant role file, or the shared contract if the rule is universal.
2. Keep role, evidence, permitted actions and completion/output sections distinct.
3. Give short examples only where they clarify an actual contract ambiguity.
4. Run `pytest`; then evaluate representative real cases for quality and cost.
5. Record the changed prompt hashes in the resulting run artifacts. Resume rejects
   checkpoints whose source/data/config/prompt context no longer matches.

Legacy `metric`, `semantic_mapping`, `generation_rule` and owner repair prompts
remain for standalone historical interfaces; they are not the default Learning
Agent's pre-extraction or repair stages. Diagnosis continues to reuse its existing
repair/ablation prompts, with suggested owner information treated as advisory.

Formatting references: [OpenAI prompt engineering](https://developers.openai.com/api/docs/guides/prompt-engineering),
[LangChain tools](https://docs.langchain.com/oss/python/langchain/tools).
