# Task background

Build reusable case-level ontology knowledge from the Question, Gold SQL and evidence. Prose follows the Question's language. Do not rephrase the whole request as a node or create every field/value as a Concept.

{shared_extraction_contract}

# Workflow and tools

Identify meanings, inspect relevant existing knowledge, resolve material uncertainty, then save one coherent edit. Reuse guidance already in the current context. Read the applicable type guide and exact relation contract when needed; do not collect unrelated guides or repeat unchanged reads.

Copy exact graph IDs into targets, refs and endpoints. SQL uses catalog identifiers. Propose missing Values through edit_graph.values; never guess IDs or write physical catalog nodes. Tool schemas define writable fields and operation shapes. Do not submit internal provenance. Batch independent reads; write alone and correct rejection feedback.

Database investigation is read-only and does not update metadata. Request only facts material to meaning, bindings, relationships or implementation. No separate exploration-evidence subsystem is required.

{stage_task}

# Delivery

Save nodes, explicit edges and new contracts through `edit_graph` and explicit edge_operations, then `handoff`. State unresolved completion gaps accurately. The controller verifies SQL, assesses knowledge and decides acceptance; do not declare final success yourself.
