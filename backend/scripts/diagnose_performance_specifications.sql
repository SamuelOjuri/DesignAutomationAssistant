-- Read-only performance-specification diagnostics for all stored active-group items.
-- Group IDs below match AUTO_SYNC_ACTIVE_GROUP_IDS; update them if settings change.
-- Membership/lifecycle comes from tasks, not a live Monday board read. Items not yet
-- discovered by ingestion are absent; stale membership requires reconciliation.
-- Landing Zone, Completed Folder and inactive lifecycle states are excluded.
-- No design-processing allowlist filter: include every stored active-group item.
-- Some legacy extraction JSON contains escaped U+0000, which PostgreSQL cannot
-- decode. Flag these payloads and skip their JSON reads; retain the item/status.
-- Pipeline identities must match in full, not only their output-vN suffix.
-- Stored extraction readiness cannot verify the current Monday email revision.
-- Artifact source identities describe the retained PDF, not the latest extraction.
-- Missing artifact source identities cannot establish freshness.
-- checked_at is the last successful specification completion, not the last attempt.
-- Generations count refresh notifications, not separate queued jobs or PDFs.
-- The lease expiry below assumes the worker's default 3600-second timeout.
-- Monday lookups come from the lifecycle and design reconciliation checks. If the
-- latest lookup returned no item (deleted and purged, or hidden from the worker),
-- extraction_diagnostic is monday_item_unavailable. Confirm the deletion in the
-- board activity log, then run backend.app.services.monday_item_retirement; retired
-- items become deleted tasks and leave this active-item report.
WITH latest_tasks AS (
    -- Keep one current observation per board/item even if account keys differ.
    SELECT DISTINCT ON (task.board_id, task.item_id)
        task.board_id,
        task.item_id,
        task.source_group_id,
        task.source_group_title,
        task.auto_sync_state
    FROM tasks AS task
    WHERE task.board_id = '1882196103'
    ORDER BY task.board_id, task.item_id, task.updated_at DESC,
        task.external_task_key
),
active_items AS (
    SELECT *
    FROM latest_tasks
    WHERE auto_sync_state = 'active'
      AND source_group_id IN ('topics', 'group_mkpbs35c', 'group_mkqbx92r')
      AND source_group_id NOT IN ('group_mkpbd6vy', 'group_mkpbb3tx')
)
SELECT
    CURRENT_TIMESTAMP AS observed_at,
    t.board_id,
    t.source_group_id,
    t.source_group_title,
    t.item_id,
    i.state AS design_state,
    monday_lookup.outcome AS monday_latest_lookup_outcome,
    GREATEST(lifecycle.last_attempted_at, design_lookup.last_attempted_at) AS monday_last_lookup_at,
    GREATEST(lifecycle.last_checked_at, design_lookup.last_checked_at) AS monday_last_seen_at,
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
        WHEN monday_lookup.outcome IN ('source_unavailable', 'unavailable')
            THEN 'monday_item_unavailable'
        WHEN i.item_id IS NULL THEN 'item_not_registered'
        WHEN i.extracted_parameters_json IS NOT NULL AND extraction.payload IS NULL
            THEN 'extraction_payload_contains_null_character'
        WHEN i.latest_desired_input_revision IS NULL THEN 'waiting_for_inputs'
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
    s.checked_at AS specification_last_success_at,
    s.scheduled_for AS specification_scheduled_for,
    s.lease_until AS specification_lease_until,
    s.attempt_count AS specification_attempt_count,
    s.review_json->>'status' AS review_status,
    s.review_json->'issues' AS review_issues,
    s.review_json->'sourceReviews' AS review_source_reviews,
    s.last_error AS specification_last_error,
    s.current_revision,
    a.status AS current_artifact_status,
    a.model_version AS artifact_model_version,
    a.source_json->>'emailRevision' AS artifact_email_revision,
    a.source_json->>'extractionPipeline' AS artifact_extraction_pipeline,
    a.filename,
    a.monday_asset_id,
    artifacts.artifact_count,
    artifacts.rendered_count,
    artifacts.published_count
FROM active_items AS t
LEFT JOIN design_processing_items AS i
    ON i.board_id = t.board_id AND i.item_id = t.item_id
LEFT JOIN auto_sync_reconciliation_checks AS lifecycle
    ON lifecycle.board_id = t.board_id
   AND lifecycle.item_id = t.item_id
   AND lifecycle.scope = 'completed_transition'
LEFT JOIN design_processing_reconciliation_checks AS design_lookup
    ON design_lookup.board_id = t.board_id AND design_lookup.item_id = t.item_id
LEFT JOIN LATERAL (
    -- The most recent lookup by either checker reflects current Monday visibility.
    SELECT observation.outcome
    FROM (VALUES
        (lifecycle.last_outcome, lifecycle.last_attempted_at),
        (design_lookup.last_outcome, design_lookup.last_attempted_at)
    ) AS observation(outcome, attempted_at)
    WHERE observation.attempted_at IS NOT NULL
    ORDER BY observation.attempted_at DESC
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
    ON s.board_id = t.board_id AND s.item_id = t.item_id
LEFT JOIN performance_specification_artifacts AS a
    ON a.board_id = s.board_id
   AND a.item_id = s.item_id
   AND a.revision = s.current_revision
LEFT JOIN LATERAL (
    SELECT job.*
    FROM design_processing_jobs AS job
    WHERE job.board_id = t.board_id AND job.item_id = t.item_id
    ORDER BY job.created_at DESC, job.id DESC
    LIMIT 1
) AS j ON TRUE
LEFT JOIN LATERAL (
    SELECT
        COUNT(*) AS artifact_count,
        COUNT(*) FILTER (WHERE artifact.status = 'rendered') AS rendered_count,
        COUNT(*) FILTER (WHERE artifact.status = 'published') AS published_count
    FROM performance_specification_artifacts AS artifact
    WHERE artifact.board_id = t.board_id AND artifact.item_id = t.item_id
) AS artifacts ON TRUE
ORDER BY t.source_group_title NULLS LAST, t.source_group_id, t.item_id;
