# Direct extraction task

Inspect this case and jointly extract reusable Concept, Metric and Generation Rule
knowledge. Existing explicit source assets are supporting input, not permission to
skip type, reference or source checks. Query the database and graph when needed.

Do not invent knowledge to fill a type quota. An empty type is valid after review.
Before ending, ensure all three types have been considered, edits are complete and
the current graph is legal. Call handoff with outcome `ready_for_validation` and
direct_type_coverage `["concept", "metric", "generation_rule"]`. If incomplete,
report `unable_to_continue`; partial edits are not automatically retained.

Reserve a model decision for this final handoff. Once all three types have been
considered and a coherent legal extraction is present, finish; do not spend the
remaining decisions rereading already available guides or polishing notes.
