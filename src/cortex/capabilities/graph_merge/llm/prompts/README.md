# Graph Merge prompts

The production bootstrap and resilient-training runner use `IncrementalIntegrationAgent` from `llm/task_agent.py`. `llm/incremental.py` is its public import boundary. One tool loop handles five roles: Identity, Node Consolidation Writer, Node Assessment, Graph Reconciliation Writer, and Graph Reconciliation Assessment.

| Role | Prompt | Result |
| --- | --- | --- |
| Identity | integration_identity.md | IdentityDecision |
| Content Writer | incremental_consolidate.md | submit_patch with MergePatch fields |
| Content Assessment | incremental_node_assess.md | Review |
| Relation Writer | incremental_relation_write.md | submit_patch with MergePatch fields |
| Relation Assessment | incremental_relation_assess.md | Review |

Content roles share `incremental_content_shared.md`; relation roles share `incremental_relation_shared.md`. All roles use `incremental_protocol.md`. Static semantic guidance appears once in the system prompt. User messages carry graph views, task obligations, differences and evidence. Public tool schemas come from Pydantic, and prompt examples are checked against those actual schemas.

Shared task tools provide inspect_graph(items/relations/consumers/contracts), lookup_catalog, read_evidence, read_guidance and inspect_draft. Identity alone uses the source-node/candidate retrieval adapter. Input, before and candidate are explicit snapshots; only compiled candidates can be independently accepted. Source dictionaries, accumulated lineage, draft versions and candidate hashes are controller-owned.

A complete submit_patch replaces the current task draft against its fixed before graph. Omitted operations are withdrawn; omitted object fields retain the before value. Structural properties replace whole values. Rejected proposals remain inspectable drafts and do not create objects. There is no submit_changes envelope, JSON Pointer correction protocol, remove_* output family or StageCompletion in this production path.

Review has verdict and issue objects. Each issue includes refs with explicit views, problem, basis and required_outcome, optionally an exact mechanically checkable absence claim. Candidate hashes are bound by the controller, not copied by the model. Original source knowledge is an integration baseline; source admission and per-Rule extraction audits are not repeated.

Stored conflicts may be factored internally. Model-facing focus branches are complete resolved content; common evidence is displayed separately from branch evidence. Physical rows show exact IDs and table/column names, with relevant grounding owners. Necessary focus/dependency fields are not replaced by cross-message position pointers.

Older integration/group workflows remain only where explicitly selected by their legacy API; they do not silently provide fallback tools or prompts for the task protocol. Changes to these production prompts are part of the task contract fingerprint; checkpoint migration must explicitly validate compatibility before reuse.

Write prose paragraphs and list items on one physical line, using editor soft wrapping. Preserve structural newlines in Markdown, JSON and SQL. When changing this contract, run the shared task-read/review, patch/compiler, checkpoint and live-tool-loop tests; mocked replay alone does not prove real model quality.
