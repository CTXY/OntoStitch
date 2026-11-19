# Merge Writer response recovery

## Observed failure

The resumed `codebase_community` batch 4 stopped during node consolidation, not identity matching or Assessment. The failed request had five messages, about 152 KB on disk, roughly 35k input tokens, disabled thinking, and `stream: false`. Three HTTP 502 responses each arrived after about 126 seconds; a later exact replay failed similarly. The configured client timeout was substantially longer. These observations are consistent with an upstream response deadline, rather than the local read timeout; the provider's internal cause remains unobservable.

A controlled replay with identical messages/tools and streaming received HTTP 200 and the first bytes after 7.1 seconds. It continued beyond the previous 126-second boundary, but produced repeated narration about whether the current candidate already existed. This baseline probe was stopped after more than 180 seconds without a result tool; partial output was not applied. No complete usage total was available.

## Small changes

1. Make the audited resume launcher select `--merge-http-mode stream` explicitly and record it. Existing streaming wire audit requires a terminal model result and preserves incomplete streams without applying them. Keep SDK retries disabled and the three-failure transport stop.
2. Clarify the shared Node Consolidation prompt: the candidate is the existing working graph after identity projection. Compare it with immutable members, submit only needed content changes, and permit empty edits when all input knowledge and differences are preserved. A matched group does not itself require a new write. Existing type-specific preservation and conflict contracts remain in force.

The latter guidance applies to every database; there are no business-domain predicates or case-ID branches. A single successful replay does not establish universal reliability or quantify the prompt's independent causal effect.

## Verification and resume

The same saved dialogue with the clarified prompt and streaming completed a valid `inspect_graph` tool request in 10.04 seconds (35,648 input / 173 output tokens). This verifies the next protocol step, not the entire merge. The unchanged stream/transport unit tests passed (20 tests). Resume from the exact batch-4 accepted graph/lineage (revision 1), backed up before launch. Preserve all prior published batches, accepted source mappings and checkpoints. Record actual runtime files/hashes plus the review-policy compatibility tag; the tag does not substitute for runtime fingerprinting. Monitor actual subsequent tool completion and publication before reporting a final merged graph.

Artifacts: `output/codebase_transport_recovery_20261117/`.
