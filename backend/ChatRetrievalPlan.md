## Plan: Bound and Diversify Chat Retrieval

Replace the eight-turn tool loop with a deterministic pipeline: one structured planning call, zero to three focused searches embedded in one batch, diversified evidence selection, and one tool-free synthesis call.

**Phase 1: Retrieval Contract**

1. Add an internal Pydantic retrieval plan in chat.py containing:
   - Planned search queries.
   - Whether a third search is justified.
   - Whether the user explicitly requests project-wide coverage.

2. Enforce server-side limits:
   - Two searches for normal questions.
   - Three searches only for compound questions.
   - Eight candidates per search.
   - Twelve evidence chunks for synthesis.
   - Three evidence chunks per file.

3. Add these validated settings to config.py.

4. Normalize and deduplicate planned queries. If planning fails, search once using the original question.

**Phase 2: Batched Retrieval**

5. Extend retrieval.py with a batch search function that:
   - Fetches the latest snapshot once.
   - Embeds all planned queries in one Gemini request.
   - Runs the existing pgvector query for each embedding.
   - Serves as the single chat retrieval entry point.

6. Include `chunkId` and matched-query metadata in internal results.

7. Merge results using query-diverse round-robin selection:
   - Deduplicate by `chunkId`.
   - Keep the lowest cosine distance for duplicates.
   - Ensure each query contributes evidence.
   - Enforce total and per-file limits.

8. Keep twelve selected chunks for synthesis, then return only the validated chunk IDs cited by the final answer to the UI.

**Phase 3: Deterministic Orchestration**

9. Preload task context directly before Gemini planning. `get_task_context` will no longer consume a tool turn.

10. Add `_plan_retrieval()` using structured output, no tools, and low temperature. It should return:
   - Zero searches for context-only questions.
   - One or two distinct searches normally.
   - Three only for independent subquestions.
   - A project-wide coverage flag for explicit inventories, audits, chronologies, project-wide contradiction searches, or absence proofs.

11. Replace `_run_with_tools()` with a bounded orchestration function:
   - Load context.
   - Plan retrieval.
   - Sanitize and deduplicate queries.
   - Run batched retrieval.
   - Select evidence.
   - Force final synthesis.

12. Remove obsolete manual tool declarations, function-response handling, turn exhaustion logic, and repeated-call behavior.

13. Refactor `_synthesize_without_tools()` into the sole synthesis path. Supply:
   - Original question and recent history.
   - Task context.
   - Planned searches.
   - Selected evidence.
   - Project-wide coverage classification.

14. Require synthesis to identify missing evidence, cite supporting chunk IDs, and qualify only explicit project-wide requests.

**Phase 4: Tests and Logging**

15. Update test_chat_bounded_retrieval.py to cover:
   - Two-query and three-query limits.
   - Query normalization and deduplication.
   - Planner failure fallback.
   - Context-only questions with no search.
   - Project-wide coverage qualification.
   - Forced synthesis and API-call counts.
   - The U-value/roof-fall compound example.

16. Add retrieval tests for batch embedding, latest-snapshot scoping, chunk deduplication, lowest-distance retention, query diversity, and evidence caps.

17. Update the orchestration mock in test_monday_first_auth.py.

18. Log planning, retrieval, selection, synthesis, and total duration. Keep query text and citation details at debug level.

**Verification**

1. Run focused tests:

   `& "venv/Scripts/python.exe" -m pytest backend/tests/test_chat_bounded_retrieval.py backend/tests/test_monday_first_auth.py -q`

2. Run syntax validation:

   `& "venv/Scripts/python.exe" -m compileall -q backend/app`

3. Run the complete suite:

   `& "venv/Scripts/python.exe" -m pytest -q`

4. Smoke-test context-only, compound, duplicate-intent, and project-wide questions through the Netlify URL. Confirm:
   - At most two generation calls.
   - At most one batched embedding call.
   - No tool-loop exhaustion warning.
   - No browser `504`.
   - Grounded citations.
   - A user-facing coverage note only for explicit project-wide requests.

This release deliberately excludes full-corpus audits, chronology generation, contradiction analysis, hybrid lexical retrieval, and migration to Gemini’s Interactions API. No database migration is required.