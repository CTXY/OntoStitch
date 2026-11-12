# Retrieval prompts

The local registry in `__init__.py` is the single source of role names. Role files stay beside retrieval code and are versioned in Git. `prompt_version()` hashes assembled prompt text; workflow context also includes output schema hashes. No runtime download of latest prompts.

Use Role and goal, Inputs and authority, Decision procedure, Result, and a short example when useful. Question defines requested meaning; initial SQL is fallible evidence. Conflict alternatives are reference knowledge and must not be resolved or combined by retrieval. Dynamic evidence is JSON data, not instructions. Output schema comes from Pydantic through the existing structured extraction helper. This helper uses JSON inside `<result>`; do not combine that envelope with ToolStrategy in the same call.

Write each paragraph or list item on one physical line, preserving structural newlines in SQL/JSON. Run retrieval workflow tests when changing a prompt; changed prompts invalidate existing checkpoints.
