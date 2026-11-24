# Task for this round

Check the local issues listed below and maintain a modification draft relative to the "local graph before modification". Signals are inspection clues; correct content can be kept; results explicitly required by the specific task must be completed and cannot be turned into optional items just because Signals are usually only suspicions.

$checks

# Processing scope and reading method

$scope

The nodes, relationships, and predicate contracts below are task data. `id` is used for exact references, `properties` is business content, and `evidence_ids` are read IDs for original evidence. Node names do not replace IDs. The physical catalog is read-only.

# Local graph before modification

The following is fixed original input (use `view="before"` when referencing). Actual results added or modified later will be placed in the next message; objects not listed as changed retain the content here.

$before_graph

# Usable relationship predicates

The first-round complete contracts include predicates used by the selected local relationships. Abstract-predicate checks also include compatible authorable candidates for the nominated endpoint types. Other installed predicates are available on demand through `read_predicates(from_node_type,to_node_type)`, including predicates for new node types. Type compatibility only means usable, not semantically relevant. Predicate reads are read-only and do not expand the graph write scope; do not infer that a predicate is not installed from its absence in the supplied contracts.

Each contract explains the predicate's meaning, direction, endpoint types, and field requirements. `authorable=false` means it can only be used to understand historical records and cannot be used to write new relationships.

$contracts

# Node field requirements

New nodes must provide complete business content according to the corresponding type; fields omitted during updates retain their original values.

$node_contracts

# Original evidence

Use `read_evidence` with displayed evidence IDs to read original Question, Gold SQL and evidence when needed. The local input is selected from one current ontology graph. No historical case graph or retired node snapshot is available.

$history
