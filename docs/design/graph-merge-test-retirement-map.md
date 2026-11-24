# Stage C test migration and retirement map

This map records removal of tests for superseded engines and protocols. It does
not claim that failure is a reason to delete a test. Current compiler, transaction,
publication, provenance and downstream retrieval behavior remains covered.

## Current coverage retained or migrated

| Retired test implementation | Current behavior and coverage |
| --- | --- |
| Old GraphIntegrationEngine and TwoStage state graphs | `test_incremental_engine.py`, `test_node_local_runtime.py`: phase order, local acceptance, resumed assessment, final publication. The old global proposal revise/revisit scheduling is intentionally retired. |
| Old Operator and NodeGroupReconciler workflows | `test_merge_identity_integration_v2.py`, `test_node_local_runtime.py`, `test_node_relation_repair.py`: complete groups, alternatives/common sources, node relation coordination and isolated nested recovery. |
| Old group checkpoint layout and trace-derived identity reads | `test_merge_task_state.py`, `test_incremental_workspace.py`, `test_single_graph_merge_regressions.py`: current state integrity and recovery. Old versions need no compatibility. |
| Old compiler publication fixtures | `test_graph_integration_commit_integrity.py`: current ActiveBatch for type/rewrite checks; aligned current snapshots for catalog mappings and conflict-name publication; repository round trip retained. |
| Old normalize_edges and node normalizer implementation | Dedicated old-algorithm tests retired rather than replaced with a different algorithm under the same claim. `test_merge_physical_ids.py` and `test_merge_join_deduplication.py` exercise current physical/publication behavior. `test_explicit_relations.py` now runs IncrementalGraphIntegrationEngine before online/retrieval projection. |
| Old identity grouping/resolver/embedding protocol | Current candidate/catalog tools remain in `test_graph_integration.py`, `test_graph_integration_local_context.py`, `test_graph_integration_targeted_reads.py`, `test_graph_integration_economy.py`. Old embeddings/group resolver contracts are intentionally retired. |
| relation_metadata authoring and group edits | Dedicated feature tests retired. `test_relation_metadata_feature.py` retains unrelated current predicate-contract checks. Multi-case/retrieval tests now use lawful node definitions and preserve their edge/source/anti-tampering intent. Historical note preservation tests remain separate from authoring. |
| Old all-role buffered submission tools | Current node-local and edit_draft contracts have dedicated agent/compiler/recovery suites. Trace/provider-thinking/SQL dialect-selection checks retained in `test_graph_merge_two_stage.py` despite the historical filename. |

Shared factories were extracted into `tests/graph_merge_fixtures.py`; tests no
longer import the old engine test module just to obtain graph constructors.
`CurrentNoopAgent` emits current typed contracts and uses the real controller,
compilers, final validation and publication gates in synchronous integration tests.

The former uncapped-malformed-output test now asserts the existing three-round
protocol recovery guard: no malformed output is accepted, calls are bounded, and
the source graph is unchanged. No production budget behavior was changed.

## Validation

The owned Stage C test set passed **375 tests**, excluding only
`test_graph_manager.py::test_self_join_is_not_accepted_as_an_ordinary_physical_join`.
That assertion already failed in the pre-refactor full baseline and concerns the
shared physical JOIN contract; neither the schema nor that test was changed.
The original full baseline's online/rule-guidance failures are separate work.

## Exact retired test functions

Names below identify the old implementation exercised by each retired test.
Parameterized cases are not individually counted. Migrated assertions and current
coverage are described above; this list does not represent 107 removed behaviors.

### test_graph_integration.py

- `test_physical_objects_cannot_be_retyped_to_bypass_read_only_protection`
- `test_identity_is_explicit_and_binding_contests_are_not_automatic_merges`
- `test_edge_to_path_has_joint_correspondence_not_fake_alias_or_source_spread`
- `test_deletion_requires_explicit_consumer_repair_and_does_not_cascade`
- `test_same_triple_different_scope_descriptions_are_not_mechanical_duplicates`
- `test_new_candidate_identity_is_retrieved_before_commit_without_source_mutation`
- `test_physical_reads_do_not_expand_the_assessment_candidate_to_the_catalog`
- `test_separate_identity_and_assessment_checkpoint_and_no_partial_publish`
- `test_rule_split_is_one_to_many_correspondence_without_single_node_alias`
- `test_business_cycle_does_not_require_topological_node_scheduling`
- `test_accept_review_cannot_contradict_itself_with_open_issues`
- `test_edge_only_update_is_visible_to_identity_and_assessment`
- `test_real_agent_wrapper_exposes_tools_and_separates_phase_histories`
- `test_provider_balance_failure_is_not_retried_as_semantic_feedback`

### test_graph_merge_two_stage.py

- `test_failed_object_repair_keeps_unrelated_edits_and_requires_fresh_revision`
- `test_nested_rule_errors_are_reported_before_graph_compilation`
- `test_rewrite_source_guard_preserves_exact_references_without_auto_propagation`
- `test_two_stage_flow_and_targeted_reassessment`
- `test_submit_tool_schema_requires_operation_and_retains_pending_repair`
- `test_read_budget_blocks_execution_not_just_advertisement`
- `test_pending_invalid_contract_can_be_retracted_without_losing_edits`
- `test_bad_correspondence_can_be_removed_and_replaced`
- `test_supported_reason_cannot_also_declare_unsupported_claims`
- `test_conflict_tool_factors_actual_actions_and_preserves_sources`

### test_graph_merge_workflow.py

- `test_agents_read_tools_and_return_source_assessment`
- `test_semantic_feedback_revises_analysis_and_candidate`
- `test_malformed_assessment_retries_only_assessment`
- `test_tool_loop_hits_shared_model_budget_and_preserves_originals`
- `test_missing_source_cannot_vacuously_pass_coverage`
- `test_group_receives_only_its_sources_from_full_run_snapshot`
- `test_invalid_semantic_structures_are_rejected`
- `test_memory_checkpoint_replays_result_without_new_calls`
- `test_sqlite_resumes_after_proposal_without_recalling_consolidator`
- `test_sqlite_merge_resume_preserves_grouping_and_validates_input`
- `test_paper_mixed_group_keeps_conditional_definition_and_complete_competitor`
- `test_native_langsmith_keeps_usage_and_hides_payloads`
- `test_equal_definitions_skip_agents_but_distinct_relations_do_not`
- `test_untouched_existing_node_is_not_reinterpreted_for_unrelated_increment`
- `test_rank_explanation_is_assessed_by_agent_not_schema`
- `test_structured_agents_allow_automatic_tools_for_reasoning_providers`
- `test_plain_text_does_not_end_role_before_structured_submission`
- `test_invalid_json_tool_call_receives_feedback_without_lenient_acceptance`
- `test_plain_text_retry_is_bounded_and_preserves_original_definitions`
- `test_conflict_checkpoint_stores_candidates_once_on_its_node`
- `test_reconciler_refuses_old_group_checkpoint_layout`

### test_graph_merge_agent_resume.py

- `test_nested_agent_resumes_tool_memory_without_repeating_completed_read`

### test_merge_transactions.py

- `test_existing_dependency_is_updated_before_its_consumer_and_preview_is_visible`
- `test_assessed_identity_correction_keeps_separate_original_nodes`
- `test_parent_checkpoint_retains_previously_committed_groups`
- `test_physical_owner_identity_is_remapped_before_matching_columns`
- `test_missing_relation_feedback_revises_candidate_before_assessment`
- `test_existing_conflict_can_be_reviewed_into_new_normal_definition`
- `test_explicit_dependency_orders_and_reassesses_existing_rule`
- `test_explicit_branch_scope_expansion_requires_assessment_approval`

### test_graph_integration_economy.py

- `test_revision_preserves_other_edits_sources_and_is_identical_to_full_proposal`
- `test_revision_created_node_can_update_then_delete_without_replay`
- `test_revision_rejects_partial_correspondence_group_replacement`
- `test_engine_revisions_show_valid_candidate_and_failed_patch_does_not_advance_it`
- `test_identity_revisit_discards_previous_candidate_and_review_focus`
- `test_revision_cannot_silently_remove_a_rewrite_destination`
- `test_revision_cannot_retype_previously_created_node_in_place`
- `test_llm_wrapper_shares_input_and_keeps_structured_submission_recovery`
- `test_artifact_audit_replays_incremental_traces_and_shared_payloads`
- `test_corrected_identity_error_is_not_replayed_as_proposal_feedback`
- `test_shared_payload_is_lossless_with_changed_and_deleted_objects`

### test_graph_integration_focus.py

- `test_edge_focus_is_full_objects_with_endpoints_and_catalog_not_unrelated_nodes`
- `test_missing_or_deleted_review_reference_falls_back_to_full_context`
- `test_consumers_excludes_adjacency_but_keeps_content_and_group_refs`
- `test_contracts_tool_respects_input_and_candidate_views`
- `test_rationale_coverage_is_required_without_claiming_semantic_truth`
- `test_submission_retry_distinguishes_invalid_json_without_repairing_it`

### test_graph_integration_assessment_scope.py

- `test_isolated_addition_without_changes_is_reviewed_before_commit`
- `test_all_edge_writes_visible_beyond_adjacency_limit`
- `test_deletions_show_before_images_and_edge_only_changes_show_endpoints`
- `test_new_relation_contract_is_visible_without_other_changes`

### test_merge_task_state.py

- `test_legacy_migration_keeps_accepted_progress_and_archives_old_reviews`
- `test_training_migration_preserves_prefix_fingerprints_and_requires_matching_plan`
- `test_migrated_outputs_cannot_start_extraction`
- `test_failed_output_copy_does_not_publish_partial_destination`

### test_common_conflict_sources.py

- `test_legacy_group_keeps_existing_common_conflict_but_refuses_reassignment`

### test_relation_metadata_feature.py

- `test_base_assets_remap_group_catalog_edge_ids`
- `test_conflict_group_remap_and_partial_projection_are_safe`
- `test_partial_selection_cannot_leave_group_pointing_at_unaccepted_edge`
- `test_group_rejects_mixed_predicate_direction_or_nonadjacent_edge`
- `test_metadata_only_change_uses_quality_injection_and_related_sql_checks`
- `test_assessment_sees_metadata_on_unchanged_owner_after_member_edit`
- `test_group_shape_and_anchor_are_checked`
- `test_export_rewrites_edge_ids_and_retains_exact_group`
- `test_projection_omits_whole_group_without_changing_original`
- `test_atomic_group_write_and_dangling_delete_rejection`

### test_relation_patch_contract_regressions.py

- `test_null_removes_optional_relation_metadata_without_removing_bindings`

### test_sync_training_merge.py

- `test_identity_uses_sync_embeddings_and_real_llm_resolver`

### test_graph_integration_commit_integrity.py

- `test_abstract_relation_is_readable_but_must_be_replaced_in_incoming_merge`
- `test_identity_alias_cannot_be_retyped_into_another_semantic_class`

### test_merge_identity_contract.py

- `test_rule_intent_is_not_a_candidate_exclusion_or_prose_validation`
- `test_concept_targets_recall_candidates_but_do_not_decide_identity`
- `test_matching_source_evidence_is_scoped_and_bounded`
- `test_missing_matching_evidence_does_not_fail_the_workflow`

### test_identity_read_recovery.py

- `test_legacy_identity_reads_require_actual_tool_results_and_a_matching_decision`

### test_compact_conflicts.py

- `test_existing_compact_conflict_can_merge_again_without_losing_candidate_ids`
- `test_identity_matching_reads_all_existing_candidates`
- `test_merge_agent_context_shares_common_content_even_for_repeated_members`

### test_merge_transaction_edges.py

- `test_preview_requires_explicit_relation_for_new_formula_operand`
- `test_conflict_preview_and_actual_commit_preserve_explicit_branch_fact`

### test_reference_relations.py

- `test_explicit_business_edge_survives_merge_normalization`

### test_concept_grounding.py

- `test_merge_keeps_explicit_connection_and_description`

### test_physical_preparation.py

- `test_graph_merge_remaps_pairs_merges_reversed_constraints_and_sources`

