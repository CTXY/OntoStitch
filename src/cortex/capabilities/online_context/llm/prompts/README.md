# Online context prompts

Prompts stay beside their owning capability. Retrieval selection belongs to `retrieve_db/llm/prompts`; the old role names forward to that registry and do not keep a second copy. This registry owns intra-node, cross-node, initial-SQL versus memory discovery, relevance audit, and confirmed guidance. Simulated-user roles are evaluation-only and excluded from the production bundle fingerprint.

Use Role and goal, Inputs and authority, Decision procedure, Constraints, Output contract, and a small example when it distinguishes a recurring failure. Existing domain rules remain authoritative. Shared Slot/Candidate rules live in `shared_slot_candidate_contract.md`; do not repeat a slightly different version in each role. Write paragraphs and list items on one physical line and preserve SQL/JSON structure.

Question and confirmed answers express intent. Ontology nodes and each alternative's own provenance are evidence. Initial SQL is an attempted implementation. A historical conflict point is a signal, not proof that the current question needs clarification. Equivalent or complementary alternatives must not become user choices. Ask about the observable business decision, retaining internal references in evidence fields.

`load_prompt`, `prompt_fingerprint`, and `prompt_version` read packaged files using importlib.resources. Version hashes cover assembled production text, including shared fragments. Online workflow identity also covers choice, answer and guidance output schemas, retrieval implementation identity, graph schema, and behavior settings. A changed identity refuses pending recovery.

Discovery Intra-node, Initial-SQL/Memory and Cross-node use direct root JSON objects with response_format=json_object, strict Pydantic validation and one bounded repair. They do not use result wrappers, XML tags or Markdown fences. Other roles retain their existing <result> text protocol. Do not combine either path with ToolStrategy output.

Run `tests/test_online_workflow_review.py`, `tests/test_online_projection.py`, and `tests/test_retrieval_online_handoff.py` after prompt or contract changes. Model behavior needs additional live evaluation; deterministic tests establish routing, preservation and projection, not language-model accuracy.
