# Real merge output protocol failures

`real_requests.json.gz` contains task payloads and model actions from the frozen
`265f78c67` runtime in `bird_merge_v3_live_20261119_154951` on 2026-11-19.
It contains no keys, authorization headers, or machine-specific input paths.

- thrombosis request 33: the initial checkpoint draft was repeated in a full
  `inspect_draft` result. Request 34 retained the same duplicate.
- thrombosis request 64: one model response supplied two `submit_patch` calls,
  `{}` and a substantive proposal. Both rejected bodies survived into the next
  active request.
- toxicology request 47: the raw SSE stream supplied distinct call indices
  0, 1 and 2: `{}`, a substantive proposal, `{}`. The old loop counted the two
  empty calls as repeated failure before a second model response was possible.
  This was present on the wire, not introduced by LangChain aggregation.

The toxicology substantive proposal also contains the unsupported field
`explanation_alt_note`. The regression first replays the exact multi-call
response, then the exact substantive proposal alone, then supplies a derived
schema repair with that extra field removed. It verifies protocol recovery and
schema validation, with a stub compiler; it does not establish semantic
acceptance of the real graph.

`checkpoints.json.gz` additionally contains exact read-only copies of the two
unfinished v3 checkpoint payloads, with original run/revision and payload SHA256.
They preserve all accepted current/history/lineage and outstanding findings.
The fixtures are regression inputs; tests write only isolated temporary copies.
Production checkpoints and frozen runtimes are never test output destinations.
