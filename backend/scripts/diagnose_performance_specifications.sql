-- Read-only specification/review diagnostics, requiring migration 0022 or later.
-- From the repository root, run:
--   venv/Scripts/python.exe -m backend.scripts.export_specification_live_items --sql
-- Run the generated SQL. For JSON input, run the exporter without --sql and
-- paste the JSON in place of {} between the $monday_snapshot$ delimiters below.
-- The supplied snapshot determines membership. {} returns no rows.
-- Refresh the snapshot before each run. It covers active items visible to the
-- Monday credential across the board. The query excludes the Completed Folder.
-- Set scope_settings from the deployed DESIGN_PROCESSING_BOARD_ID,
-- DESIGN_PROCESSING_LANDING_GROUP_ID, AUTO_SYNC_ACTIVE_GROUP_IDS and
-- AUTO_SYNC_COMPLETED_GROUP_ID. Supply the
-- full deployed settings.design_processing_pipeline_version when checking upgrades;
-- NULL deliberately reports that the target pipeline has not been verified.
-- For worker-specific availability, set processing_identity to the fingerprint
-- returned by backend.app.services.monday_item_availability.processing_identity()
-- in that worker's environment. NULL shows the latest stored observation across
-- processing identities. Do not put the Monday access token in this query.
-- Version settings below match the source/parser/evidence constants in this release.
-- Task, worker availability and reconciliation data are stored diagnostics for
-- the live membership. Publication mode labels describe the group policy.
-- Operational settings and permissions also govern publication and completion.
-- Some legacy extraction JSON contains escaped U+0000, which PostgreSQL cannot
-- decode. Flag these payloads and skip their JSON reads; retain the item/status.
-- Pipeline identities must match in full, not only their output-vN suffix.
-- Stored extraction readiness cannot verify the current Monday email revision.
-- Artifact source identities describe the retained PDF, not the latest extraction.
-- Missing artifact source identities cannot establish freshness.
-- Generations count refresh notifications, not separate queued jobs or PDFs.
-- The design-job lease expiry below assumes its default 3600-second timeout;
-- specification_lease_until is the persisted specification worker lease deadline.
WITH scope_settings AS (
    SELECT
        '1882196103'::text AS board_id,
        'group_mkpbd6vy'::text AS landing_group_id,
        ARRAY['topics','group_mkpbs35c','group_mkqbx92r']::text[] AS active_group_ids,
        'group_mkpbb3tx'::text AS completed_group_id,
        NULL::text AS expected_pipeline_version,
        NULL::text AS processing_identity,
        '3'::text AS expected_evidence_version,
        'specification-active-review-v2'::text AS expected_scope_policy_version,
        'performance-specification-v5'::text AS expected_publication_version,
        'specification-review-source-v1'::text AS expected_source_policy_version,
        'specification-reference-v1'::text AS expected_reference_parser_version
),
monday_snapshot AS (
    SELECT $monday_snapshot${}$monday_snapshot$::jsonb AS payload
),
live_items AS (
    SELECT DISTINCT cfg.board_id, snapshot.observed_at, item.item_id, item.group_id, item.group_title
    FROM scope_settings AS cfg
    CROSS JOIN monday_snapshot AS input
    CROSS JOIN LATERAL jsonb_to_record(input.payload)
        AS snapshot(board_id text, observed_at timestamptz, items jsonb)
    CROSS JOIN LATERAL jsonb_to_recordset(snapshot.items)
        AS item(item_id text, group_id text, group_title text)
    WHERE snapshot.board_id = cfg.board_id
      AND snapshot.observed_at IS NOT NULL
      AND item.group_id IS DISTINCT FROM cfg.completed_group_id
      AND item.item_id ~ '^[1-9][0-9]*$'
      AND NULLIF(btrim(item.group_id), '') IS NOT NULL
),
latest_tasks AS (
    -- Keep one Task per live item even if account keys differ.
    SELECT DISTINCT ON (task.board_id, task.item_id)
        task.board_id,
        task.item_id,
        task.source_group_id,
        task.source_group_title,
        task.auto_sync_state
    FROM tasks AS task
    JOIN live_items AS live ON live.board_id = task.board_id AND live.item_id = task.item_id
    ORDER BY task.board_id, task.item_id, task.updated_at DESC,
        task.external_task_key
),
latest_availability AS (
    SELECT DISTINCT ON (observation.board_id, observation.item_id)
        observation.board_id,
        observation.item_id,
        observation.state,
        observation.reason,
        observation.actual_board_id,
        observation.actual_group_id,
        observation.actual_item_state,
        observation.last_attempted_at,
        observation.last_observed_at,
        observation.next_check_at,
        observation.probe_until
    FROM monday_item_availability AS observation
    JOIN live_items AS live ON live.board_id = observation.board_id AND live.item_id = observation.item_id
    JOIN scope_settings AS cfg ON cfg.board_id = observation.board_id
    WHERE cfg.processing_identity IS NULL OR observation.processing_identity = cfg.processing_identity
    ORDER BY observation.board_id, observation.item_id,
        GREATEST(observation.last_attempted_at, observation.last_observed_at) DESC NULLS LAST,
        observation.processing_identity
)
SELECT
    CURRENT_TIMESTAMP AS observed_at,
    ids.observed_at AS monday_snapshot_observed_at,
    ids.board_id,
    ids.item_id,
    ids.group_id AS live_group_id,
    ids.group_title AS live_group_title,
    COUNT(*) OVER (PARTITION BY ids.group_id) AS live_group_item_count,
    CASE
        WHEN ids.group_id = cfg.landing_group_id THEN 'landing_zone_automatic_publication'
        WHEN ids.group_id = ANY(cfg.active_group_ids) THEN 'active_group_manual_publication'
        ELSE 'outside_specification_groups'
    END AS live_scope_diagnostic,
    s.prepared_revision,
    draft.status AS prepared_artifact_status,
    draft.source_json->'review'->>'publication_identity' AS prepared_publication_identity,
    s.requested_publication_revision,
    t.source_group_id,
    t.source_group_title,
    t.auto_sync_state AS task_auto_sync_state,
    i.state AS design_state,
    availability.state AS monday_availability_state,
    availability.actual_board_id AS monday_observed_board_id,
    availability.actual_group_id AS monday_observed_group_id,
    availability.actual_item_state AS monday_observed_item_state,
    availability.next_check_at AS monday_availability_next_check_at,
    availability.probe_until AS monday_availability_probe_until,
    (cfg.processing_identity IS NOT NULL) AS monday_availability_identity_configured,
    monday_lookup.source AS monday_lookup_source,
    monday_lookup.outcome AS monday_latest_lookup_outcome,
    monday_lookup.reason AS monday_lookup_reason,
    monday_lookup.attempted_at AS monday_last_lookup_at,
    CASE
        WHEN monday_lookup.outcome IN ('source_unavailable', 'unavailable') THEN 'monday_item_unavailable'
        WHEN monday_lookup.outcome IN ('archived', 'deleted', 'out_of_board', 'retired')
            THEN 'monday_item_' || monday_lookup.outcome
    END AS stored_monday_diagnostic,
    GREATEST(
        CASE WHEN availability.state <> 'retired' THEN availability.last_observed_at END,
        lifecycle.last_checked_at, active_lookup.last_checked_at, design_lookup.last_checked_at
    ) AS monday_last_seen_at,
    s.review_json->'source' AS review_source,
    i.latest_desired_input_revision,
    i.latest_analyzed_input_revision,
    i.latest_desired_pipeline_version,
    i.latest_analyzed_pipeline_version,
    extraction.payload->>'inputRevision' AS extraction_input_revision,
    extraction.payload->>'pipelineVersion' AS extraction_pipeline_version,
    extraction.payload->>'specificationEvidenceVersion' AS evidence_version,
    (i.extracted_parameters_json IS NOT NULL AND extraction.payload IS NULL)
        AS extraction_json_has_null_escape,
    CASE
        WHEN i.item_id IS NULL THEN 'item_not_registered'
        WHEN i.extracted_parameters_json IS NOT NULL AND extraction.payload IS NULL
            THEN 'extraction_payload_contains_null_character'
        WHEN i.latest_desired_input_revision IS NULL THEN 'waiting_for_inputs'
        WHEN cfg.expected_pipeline_version IS NOT NULL
         AND i.latest_desired_pipeline_version IS DISTINCT FROM cfg.expected_pipeline_version
            THEN 'desired_pipeline_obsolete'
        WHEN i.latest_analyzed_input_revision IS DISTINCT FROM i.latest_desired_input_revision
            THEN 'waiting_for_input_revision'
        WHEN i.latest_analyzed_pipeline_version IS DISTINCT FROM i.latest_desired_pipeline_version
            THEN 'waiting_for_pipeline_version'
        WHEN (extraction.payload->>'inputRevision')
                IS DISTINCT FROM i.latest_analyzed_input_revision
          OR (extraction.payload->>'pipelineVersion')
                IS DISTINCT FROM i.latest_analyzed_pipeline_version
            THEN 'extraction_payload_identity_mismatch'
        WHEN extraction.payload->>'specificationEvidenceVersion' IS NULL
            THEN 'missing_evidence_version'
        WHEN extraction.payload->>'specificationEvidenceVersion'
                IS DISTINCT FROM cfg.expected_evidence_version THEN 'evidence_version_obsolete'
        WHEN cfg.expected_pipeline_version IS NULL THEN 'target_pipeline_not_supplied_check_live_sources'
        ELSE 'stored_identities_current_check_live_sources'
    END AS extraction_diagnostic,
    j.id AS latest_job_id,
    j.status AS latest_job_status,
    j.stage AS latest_job_stage,
    j.execution_kind,
    j.execution_pipeline_version,
    j.attempt_count AS job_attempt_count,
    j.scheduled_for AS job_scheduled_for,
    j.next_retry_at,
    j.heartbeat_at,
    CASE WHEN j.status = 'running'
        THEN COALESCE(j.heartbeat_at, j.locked_at) + INTERVAL '1 hour'
    END AS default_job_lease_expires_at,
    j.last_error AS job_last_error,
    s.requested_generation,
    s.completed_generation,
    s.checked_at AS specification_checked_at,
    s.scheduled_for AS specification_scheduled_for,
    s.lease_until AS specification_lease_until,
    s.attempt_count AS specification_attempt_count,
    s.review_json->>'status' AS review_status,
    s.review_json->>'reason' AS review_reason,
    s.review_json->'issues' AS review_issues,
    s.review_json->'sourceReviews' AS review_source_reviews,
    s.last_error AS specification_last_error,
    s.current_revision,
    s.source_json->>'scopePolicy' AS last_verified_scope_policy,
    s.source_json->>'publicationVersion' AS last_verified_publication_version,
    s.source_json->>'extractionPipeline' AS last_verified_extraction_pipeline,
    CASE
        WHEN s.item_id IS NULL THEN 'not_discovered'
        WHEN s.source_json->>'scopePolicy' IS DISTINCT FROM cfg.expected_scope_policy_version
          OR s.source_json->>'publicationVersion' IS DISTINCT FROM cfg.expected_publication_version
          OR s.source_json->>'sourcePolicyVersion' IS DISTINCT FROM cfg.expected_source_policy_version
          OR s.source_json->>'referenceParserVersion' IS DISTINCT FROM cfg.expected_reference_parser_version
          OR NULLIF(s.source_json->>'extractionPipeline', '') IS NULL
          OR (cfg.expected_pipeline_version IS NOT NULL
              AND s.source_json->>'extractionPipeline' IS DISTINCT FROM cfg.expected_pipeline_version)
            THEN 'refresh_policy_obsolete'
        WHEN cfg.expected_pipeline_version IS NULL THEN 'target_pipeline_not_supplied_check_live_sources'
        ELSE 'stored_refresh_policy_current_check_live_sources'
    END AS refresh_policy_diagnostic,
    a.status AS current_artifact_status,
    a.model_version AS artifact_model_version,
    a.source_json->>'emailRevision' AS artifact_email_revision,
    a.source_json->>'extractionPipeline' AS artifact_extraction_pipeline,
    a.source_json->>'sourcePolicyVersion' AS artifact_source_policy_version,
    a.source_json->>'referenceParserVersion' AS artifact_reference_parser_version,
    CASE
        WHEN s.item_id IS NULL THEN 'not_discovered'
        WHEN a.revision IS NULL THEN 'no_current_published_artifact'
        WHEN NULLIF(a.source_json->>'emailRevision', '') IS NULL
          OR NULLIF(a.source_json->>'extractionPipeline', '') IS NULL THEN 'artifact_source_identity_missing'
        WHEN a.source_json->>'publicationVersion' IS DISTINCT FROM cfg.expected_publication_version
          OR a.source_json->>'sourcePolicyVersion'
                IS DISTINCT FROM cfg.expected_source_policy_version
          OR a.source_json->>'referenceParserVersion'
                IS DISTINCT FROM cfg.expected_reference_parser_version THEN 'artifact_policy_obsolete'
        WHEN (cfg.expected_pipeline_version IS NOT NULL
              AND a.source_json->>'extractionPipeline' IS DISTINCT FROM cfg.expected_pipeline_version)
          OR (i.latest_desired_input_revision IS NOT NULL AND (
                  a.source_json->>'emailRevision' IS DISTINCT FROM i.latest_desired_input_revision
               OR a.source_json->>'extractionPipeline' IS DISTINCT FROM i.latest_desired_pipeline_version))
            THEN 'artifact_extraction_obsolete'
        WHEN i.latest_desired_input_revision IS NULL OR i.latest_analyzed_input_revision IS NULL
            THEN 'artifact_extraction_identity_unknown'
        WHEN a.source_json->>'emailRevision' IS DISTINCT FROM i.latest_analyzed_input_revision
          OR a.source_json->>'extractionPipeline' IS DISTINCT FROM i.latest_analyzed_pipeline_version
            THEN 'artifact_extraction_obsolete'
        WHEN cfg.expected_pipeline_version IS NULL THEN 'target_pipeline_not_supplied_check_live_sources'
        ELSE 'stored_artifact_identity_current_check_live_sources'
    END AS artifact_freshness_diagnostic,
    a.filename,
    a.monday_asset_id,
    r.monday_account_id AS review_account_id,
    r.review_version,
    r.selected_request_type,
    r.selected_project_board_id,
    r.selected_project_item_id,
    r.selected_drawing_board_id,
    r.selected_drawing_item_id,
    r.source_fingerprint AS review_source_fingerprint,
    r.decision_digest AS review_decision_digest,
    r.desired_publication_identity,
    a.source_json->'review'->>'publication_identity' AS artifact_publication_identity,
    a.source_json->'review'->>'review_version' AS artifact_review_version,
    a.source_json->'review'->>'decision_digest' AS artifact_decision_digest,
    r.current_approval_id,
    approval.artifact_revision AS approved_artifact_revision,
    approval.review_version AS approved_review_version,
    approval.source_fingerprint AS approved_source_fingerprint,
    approval.actor_monday_user_id AS approving_monday_user_id,
    approval.approved_at,
    COALESCE(
        approval.artifact_revision = s.current_revision
        AND (s.prepared_revision IS NULL OR s.prepared_revision = s.current_revision)
        AND approval.review_version = r.review_version
        AND approval.source_fingerprint = r.source_fingerprint
        AND approval.decision_digest = r.decision_digest
        AND a.source_json->'review'->>'review_version' = r.review_version::text
        AND a.source_json->'review'->>'decision_digest' = r.decision_digest
        AND a.source_json->'review'->'source_identity'->>'source_fingerprint' = r.source_fingerprint
        AND a.source_json->'review'->'source_identity'->>'extraction_identity' = approval.extraction_identity
        AND r.source_identity_json->>'extraction_identity' = approval.extraction_identity
        AND a.source_json->'review'->>'publication_identity' = r.desired_publication_identity
        AND a.status = 'published' AND NULLIF(a.monday_asset_id, '') IS NOT NULL
        AND NULLIF(a.content_sha256, '') IS NOT NULL AND a.size_bytes > 0
        AND s.requested_generation = s.completed_generation AND s.lease_token IS NULL,
        FALSE
    ) AS approval_matches_stored_identity,
    history.approval_count,
    history.last_approved_at,
    artifacts.artifact_count,
    artifacts.rendered_count,
    artifacts.published_count
FROM live_items AS ids
JOIN scope_settings AS cfg ON cfg.board_id = ids.board_id
LEFT JOIN latest_tasks AS t ON t.board_id = ids.board_id AND t.item_id = ids.item_id
LEFT JOIN design_processing_items AS i
    ON i.board_id = ids.board_id AND i.item_id = ids.item_id
LEFT JOIN latest_availability AS availability
    ON availability.board_id = ids.board_id AND availability.item_id = ids.item_id
LEFT JOIN auto_sync_reconciliation_checks AS lifecycle
    ON lifecycle.board_id = ids.board_id
   AND lifecycle.item_id = ids.item_id
   AND lifecycle.scope = 'completed_transition'
LEFT JOIN auto_sync_reconciliation_checks AS active_lookup
    ON active_lookup.board_id = ids.board_id
   AND active_lookup.item_id = ids.item_id
   AND active_lookup.scope = 'active'
LEFT JOIN design_processing_reconciliation_checks AS design_lookup
    ON design_lookup.board_id = ids.board_id AND design_lookup.item_id = ids.item_id
LEFT JOIN LATERAL (
    -- Shared availability is authoritative for its processing identity. Its
    -- observation can predate the bookkeeping timestamp on a legacy check.
    SELECT observation.source, observation.outcome, observation.reason, observation.attempted_at
    FROM (VALUES
        ('availability', NULLIF(availability.state, 'unknown'), availability.reason, availability.last_attempted_at),
        ('completed_transition', lifecycle.last_outcome, lifecycle.last_reason, lifecycle.last_attempted_at),
        ('active_reconciliation', active_lookup.last_outcome, active_lookup.last_reason, active_lookup.last_attempted_at),
        ('design_reconciliation', design_lookup.last_outcome, design_lookup.last_reason, design_lookup.last_attempted_at)
    ) AS observation(source, outcome, reason, attempted_at)
    WHERE observation.attempted_at IS NOT NULL
      AND observation.outcome IS NOT NULL
      AND NOT COALESCE(
          observation.source <> 'availability' AND observation.outcome = 'retired'
          AND availability.state IN ('available', 'archived', 'deleted', 'out_of_board')
          AND availability.last_observed_at >= observation.attempted_at,
          FALSE
      )
    ORDER BY CASE WHEN observation.outcome = 'retired' THEN 0
                  WHEN observation.source = 'availability' THEN 1 ELSE 2 END,
        observation.attempted_at DESC, observation.source
    LIMIT 1
) AS monday_lookup ON TRUE
LEFT JOIN LATERAL (
    SELECT CASE
        -- An odd run of backslashes followed by u0000 encodes a JSON null
        -- character. Even runs encode literal backslashes and remain readable.
        WHEN i.extracted_parameters_json::text ~ $json$(?<!\\)(?:\\\\)*\\u0000$json$
            THEN NULL
        ELSE i.extracted_parameters_json
    END AS payload
) AS extraction ON TRUE
LEFT JOIN performance_specifications AS s
    ON s.board_id = ids.board_id AND s.item_id = ids.item_id
LEFT JOIN performance_specification_reviews AS r
    ON r.board_id = ids.board_id AND r.item_id = ids.item_id
LEFT JOIN performance_specification_approvals AS approval
    ON approval.id = r.current_approval_id
   AND approval.monday_account_id = r.monday_account_id
   AND approval.board_id = r.board_id AND approval.item_id = r.item_id
LEFT JOIN performance_specification_artifacts AS a
    ON a.board_id = s.board_id
   AND a.item_id = s.item_id
   AND a.revision = s.current_revision
LEFT JOIN performance_specification_artifacts AS draft
    ON draft.board_id = s.board_id
   AND draft.item_id = s.item_id
   AND draft.revision = s.prepared_revision
LEFT JOIN LATERAL (
    SELECT job.*
    FROM design_processing_jobs AS job
    WHERE job.board_id = ids.board_id AND job.item_id = ids.item_id
    ORDER BY job.created_at DESC, job.id DESC
    LIMIT 1
) AS j ON TRUE
LEFT JOIN LATERAL (
    SELECT
        COUNT(*) AS artifact_count,
        COUNT(*) FILTER (WHERE artifact.status = 'rendered') AS rendered_count,
        COUNT(*) FILTER (WHERE artifact.status = 'published') AS published_count
    FROM performance_specification_artifacts AS artifact
    WHERE artifact.board_id = ids.board_id AND artifact.item_id = ids.item_id
) AS artifacts ON TRUE
LEFT JOIN LATERAL (
    SELECT COUNT(*) AS approval_count, MAX(prior.approved_at) AS last_approved_at
    FROM performance_specification_approvals AS prior
    WHERE prior.board_id = ids.board_id AND prior.item_id = ids.item_id
) AS history ON TRUE
ORDER BY ids.group_id, ids.item_id;
