# Merge prompts

`IncrementalIntegrationAgent` uses these active entry points:

| Role | System construction | Public result |
| --- | --- | --- |
| Identity | integration_identity.md + incremental_protocol.md | IdentityDecision |
| Node Writer | node_local/common-fields.zh.md → writer.system.zh.md → writer-output-contract.zh.md → types/{current type}.zh.md | submit_patch with NodeWriterResult |
| Node Assessment | node_local/common-fields.zh.md → assessment.system.zh.md → assessment-output-contract.zh.md → types/{current type}.zh.md | Review with NodeAssessmentResult |
| Relation Writer | reconciliation/prompts/common.zh.md → writer.zh.md → writer-contract.zh.md → current Signal shared/Writer guidance | edit_draft；编译成功自动进入 Assessment |
| Graph Assessment | reconciliation/prompts/common.zh.md → assessment.zh.md → assessment-contract.zh.md → current Signal shared/Assessment guidance | Review with per-Signal and prior-issue results |

The two node roles expose only `read_evidence` and their result tool. They receive one complete identity group, compact outgoing relations and required endpoint summaries. Type guidance is always last; node tools remain identical across node types. See [node_local/README.md](node_local/README.md) for editable files.

The former `incremental_consolidate.md`, `incremental_node_assess.md` and `incremental_content_shared.md` are historical assets retained with prior edits. They are no longer loaded or included in the active contract fingerprint. Review drafts in docs/design/node-consolidation-local-review are also not runtime inputs.

User messages contain controller data. Read evidence is kept once per ID; current candidate/draft and bound diagnostics occupy the final message. Node relation followups are separately persisted and independently assessed before final publication.

The relation roles expose only read_nodes/read_evidence/request_context plus their own editing/result tools. They reconstruct six messages from current local views and one canonical draft; no graph inspection tool or legacy whole-Patch result is registered. See reconciliation/README.md. Production assets live here, not in design docs.

## English runtime policies

Node consolidation, relation merger and reconciliation now load the `.en.md`
assets. The `.zh.md` copies are earlier review documents, not active policies.
Node type guidance remains last, after the shared role prefix. Reconciliation
keeps its fixed system/task prefix and replaces the current-state message each
round. `schema-contracts.en.json` supplies English prose for known schema
metadata only; it never rewrites source graphs or QA. The asset and projection
code are included in checkpoint policy hashes, so old checkpoints require an
explicit migration. This prompt change does not restart background workers.
