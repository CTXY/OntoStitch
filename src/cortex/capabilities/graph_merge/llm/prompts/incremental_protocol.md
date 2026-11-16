## Response protocol

Use tools for missing information and submit through the result tool. Keep reasons concise in the required result fields; do not narrate analysis before tool calls or repeat supplied graph/evidence. Tool arguments and results carry the working state. Plain JSON is reserved for an explicitly requested malformed-tool recovery.

Read only necessary missing objects or fields. An identical read at the same draft revision returns `already_read`; use its earlier result. Different required reads remain available, including after several tool rounds. An unshown operand is not proof that it does not exist; inspect the relevant context before declaring an unrepresentable repair.
