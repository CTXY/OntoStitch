# Graph Merge prompts

`PROMPTS` in `__init__.py` is the only role registry. `load_prompt` composes a role file, `shared.md`, and one `merge_types/` contract; identity uses only its own role file. Prompts stay versioned with code; `prompt_fingerprint` records the final rendered content in LangSmith metadata and checkpoints. No runtime download of latest prompts or second registry.

Use consistent sections: Role and goal, Inputs, Task/Assessment, Result, and a small example when it resolves a recurring ambiguity. Keep shared evidence/preservation rules in shared.md and type requirements in merge_types; avoid copying them into both roles. Dynamic evidence is serialized inside an explicit <merge_context> block and must not be treated as instructions. The response schema is generated from Pydantic rather than duplicated as a hand-maintained JSON schema.

When changing prompts, run the group-workflow tests and the N1/N2/N3 integration examples. A prompt change invalidates resuming a run made with a different prompt version. Record the hash, not the full source SQL, in remote trace metadata.

References informing this format:
- https://docs.anthropic.com/en/docs/build-with-claude/prompt-engineering/overview
- https://docs.langchain.com/oss/python/langchain/structured-output

## Formatting

Write each prose paragraph and each list item on one physical line; use editor soft wrapping for display. Keep blank lines between sections and paragraphs. Preserve structural newlines and indentation in headings, nested lists, fenced examples, JSON/SQL and output envelopes. Apply the same convention to shared contracts, YAML prompt blocks and model-visible tool descriptions.
