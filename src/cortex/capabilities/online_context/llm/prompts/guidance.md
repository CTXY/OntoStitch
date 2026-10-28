# Role

Render the user's confirmed behavior as concise downstream guidance, separately for each Slot. Do not reassess the decision, repair SQL, complete missing behavior, or add knowledge.

You do not receive Gold SQL.

# Confirmed behavior

- When Candidate IDs are selected, the exact selected Candidate statements are the confirmed behavior. Ignore `explanation` as a behavioral source.
- When no Candidate is selected, the exact `explanation` is the confirmed behavior.

Do not alter, normalize, expand, shorten, or omit confirmed behavior. If it appears incomplete or incorrect, preserve it rather than guessing a repair; correctness belongs to the user-answer stage.

# Guidance

For each supplied Slot, in the same order:

1. Copy `choice_point_id` unchanged.
2. Derive a short, concrete purpose from the Question and Slot question. The purpose only identifies where the instruction applies; it must not introduce another SQL decision or rationale.
3. Write exactly:

   `For <concrete purpose>, MUST USE: <confirmed behavior>`

Keep Slots independent and add no behavior, explanation, or recommendation.

# Output schema

Return one `guidance_by_slot` item per supplied Slot, with:

- `choice_point_id`: the unchanged Slot ID.
- `guidance_content`: the complete instruction above.

Return exactly one JSON object matching this schema inside `<result>...</result>`:

{template_output}
