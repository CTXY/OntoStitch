# Real accepted-signal replay fixture

`toxicology_repeated_signals.json.gz` records five checks that were accepted and
then triggered again in toxicology batch 2 on 2026-11-22. It contains eight
distinct actual Writer input snapshots, their original grouping, the exact
independent Assessment acceptance outputs, and 28 original evidence records.
Same-task Writer revisions requested by Assessment are excluded from the pairs.

The `graph` field preserves the actual rendered local input. `graph_state` is a
validated model projection for deterministic code regression: all rendered
business fields and relations are retained, public evidence IDs are represented
as canonical QA `SourceReference` objects, and complete physical directory nodes
are copied from frozen batch artifacts. It is **not** the full authoritative
ontology workspace, and must not be used to prove global absence. Storage-level
provenance variants that were not visible in the public input are not invented.

The complete frozen physical catalog is deliberately present in every snapshot
so that missing table/column references are not patched with fabricated objects.
Unrelated physical material must not enter an individual signal premise.

`manifest.json` binds the compressed fixture and uncompressed payload by SHA256.
Its source record binds the captured prefix of the append-only runtime log;
later runtime activity does not invalidate the captured historical evidence.

To regenerate from the original workspace, without model calls or mutations:

```sh
PYTHONPATH=src .venv/bin/python tests/fixtures/reconciliation_signal_receipts/build_fixture.py /path/to/original/workspace
```

The original output records and checkpoints are only read. No duplicated input
groups, simulated model acceptances, or new API requests are used.
