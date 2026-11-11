"""Email-first recommendations and effective, PDF-only reviewer values.

The source snapshot keeps automatic recommendations apart from saved decisions.
Only the holding item's live identity and input revision are mandatory remote
reads. Optional CRM failures remain visible without erasing authoritative edits.
"""

from __future__ import annotations

from copy import deepcopy

from ..config import settings
from .performance_specification_contract import SOURCE_REVIEW_DETAILS, missing, reviewer_cad_mapping
from .performance_specification_evidence import (
    PAGE_SIZE_CONFLICT, PAGE_SIZE_UNCERTAIN, UNDATED_SCOPE_CONFLICT, VALUE_CONFLICT,
    UNCONFIRMED_REFERENCE, resolve_baseline_drawing_identity, resolve_email_evidence, resolve_request_notes,
)
from .specification_references import (
    REFERENCE_PARSER_VERSION, drawing_references_equal, recommend_drawing_candidates,
)
from .specification_review_artifacts import review_digest
from .specification_review_contract import (
    FIELD_NAMES, FIELD_POLICY, SOURCE_POLICY_VERSION, AutomaticProposal, CADMapping,
    CandidateLookup, CRMFieldRead, CRMRecommendation, DrawingCandidate, EmailCitation,
    EmailFact, FieldIssue, MondayRecord, ProjectMatch, RequestIntentRecommendation,
    ReviewDecisionState, ReviewFieldSources, ReviewSourceSnapshot, SourceIdentity,
    SourceIssue, effective_field_value,
)
from .specification_semantics import tp_reference_revision
from .specification_policy import SPECIFICATION_SCOPE_POLICY_VERSION, specification_scope_exclusion


def _citations(provenance):
    candidates = provenance.get("candidates", [])
    return [EmailCitation(source_id=entry["source_id"], excerpt=entry["snippet"],
        location=entry["location"], scope=entry.get("scope")) for entry in candidates]


def _email_facts(provenance):
    return [EmailFact(value=entry["value"], evidence=[EmailCitation(
        source_id=entry["source_id"], excerpt=entry["snippet"], location=entry["location"],
        scope=entry.get("scope"))]) for entry in provenance.get("candidates", [])]


def resolve_review_sources(gateway, item, previous=None, *, review_state=None, preview=False):
    # Import the shared gateway parsers lazily: their public entry point delegates
    # here while candidate preparation still uses the same verified column reads.
    from .performance_specification_sources import (
        SpecificationIneligible, SpecificationSourceError, SpecificationSourceWaiting, SpecificationValueConflict,
        SpecificationReviewRequired, UNMATCHED_DRAWING, UNASSIGNED_DRAWING,
        _columns, _find_column, _normal, _project_display_labels, _read_column,
    )
    from .specification_request_context import request_context_summary, select_client_request

    state = review_state or ReviewDecisionState()
    for scope, mappings in settings.performance_specification_column_map.items():
        if set(mappings) - set(FIELD_NAMES):
            raise SpecificationSourceError(f"Unknown specification field in {scope} column mapping")
    target = gateway.fetch_target(item.item_id)
    if (str(target.item_id) != str(item.item_id)
            or specification_scope_exclusion(target) is not None):
        raise SpecificationIneligible("Specification item is outside the live Landing Zone scope")
    extraction = item.extracted_parameters_json or {}
    if extraction.get("pipelineVersion") != settings.design_processing_pipeline_version:
        raise SpecificationSourceWaiting("Waiting for extraction from the current configured pipeline")
    if (not target.input_revision or extraction.get("inputRevision") != target.input_revision
            or (preview and (extraction.get("inputRevision") != item.latest_desired_input_revision
                or extraction.get("pipelineVersion") != item.latest_desired_pipeline_version))
            or (not preview and (item.latest_analyzed_input_revision != item.latest_desired_input_revision
                or item.latest_analyzed_pipeline_version != item.latest_desired_pipeline_version
                or extraction.get("pipelineVersion") != item.latest_analyzed_pipeline_version))):
        raise SpecificationSourceWaiting("Waiting for current email extraction")
    if not isinstance(extraction.get("performanceParameters"), dict) or not extraction["performanceParameters"]:
        raise SpecificationSourceWaiting("Waiting for evidence-aware email extraction")
    try:
        email_raw, email_sources, email_issues = resolve_email_evidence(extraction)
        request_notes = resolve_request_notes(extraction)
        intent = RequestIntentRecommendation.model_validate(extraction.get("specificationRequestIntent") or {
            "recommended_type": "uncertain", "explanation": "Request type requires reviewer classification",
        })
    except ValueError as exc:
        raise SpecificationSourceError(str(exc)) from exc

    # Holding Accounts, Date Received and TP Ref are human-owned context. Their
    # contents are deliberately absent from the semantic inputs and dependencies.
    holding = gateway.fetch_holding(item.item_id)
    if (str((holding.get("board") or {}).get("id")) != target.board_id
            or str(holding.get("id")) != str(item.item_id)
            or specification_scope_exclusion(board_id=(holding.get("board") or {}).get("id"),
                group_id=(holding.get("group") or {}).get("id"), item_state=holding.get("state")) is not None):
        raise SpecificationIneligible("Specification holding item changed scope")

    request_type = state.selections.selected_request_type or (
        intent.recommended_type if intent.recommended_type != "uncertain" else None)
    match_payload = getattr(item, "match_result_json", None) or {}
    # Ignore a matching sidecar belonging to an older extraction/input.
    if any(match_payload.get(key) not in {None, extraction.get(key)}
           for key in ("inputRevision", "pipelineVersion")):
        match_payload = {}
    match_contract = match_payload.get("result") or {}
    candidate_context = match_contract.get("specificationCandidates") or {}
    candidate_lookup = CandidateLookup.model_validate(candidate_context.get("lookup") or {
        "status": "pending" if preview and not candidate_context else "available",
        "projects": [entry.model_dump(mode="json") for entry in intent.matched_projects],
    })
    projects = {entry.record.item_id: entry for entry in candidate_lookup.projects}
    for entry in intent.matched_projects:
        existing = projects.setdefault(entry.record.item_id, entry.model_copy())
        for label in ("project_reference", "project_title"):
            if not getattr(existing, label) and getattr(entry, label):
                setattr(existing, label, getattr(entry, label))
    for entry in match_contract.get("candidates", []):
        identity = str(entry.get("mondayItemId") or "")
        if identity.isdigit() and int(identity):
            matched_project = projects.setdefault(identity, ProjectMatch(record={
                "board_id": str(settings.design_processing_project_board_id), "item_id": identity},
                rationale=entry.get("matchRationale") or "Project match; supporting context only",
                similarity_score=entry.get("similarity")))
            # Older cached lookup sidecars lack labels. Enrich those entries too,
            # using the matching metadata already stored for this extraction.
            for label, value in _project_display_labels(entry).items():
                if not getattr(matched_project, label) and value:
                    setattr(matched_project, label, value)
    candidate_lookup.projects = list(projects.values())
    dependencies = set()
    crm_reads, crm_values, crm_errors = [], {}, {}
    selected_project = state.selections.selected_project
    if selected_project is None and request_type == "amendment" and len(projects) == 1:
        selected_project = next(iter(projects.values())).record
    if selected_project and selected_project.board_id != str(settings.design_processing_project_board_id):
        raise SpecificationSourceError("Selected project is outside the configured project board")
    selected_drawing_record = state.selections.selected_drawing
    if state.selections.selected_project is None:
        # Retain all plausible projects and their source records until a reviewer
        # narrows the choice. The parent dependency also catches new subitems.
        dependencies.update((entry.record.board_id, entry.record.item_id)
            for entry in candidate_lookup.projects)
        for candidate in candidate_lookup.drawings:
            dependencies.add((candidate.project.board_id, candidate.project.item_id))
            dependencies.add((candidate.record.board_id, candidate.record.item_id))
        for identity in candidate_context.get("dependencies", []):
            if (isinstance(identity, (list, tuple)) and len(identity) == 2
                    and all(str(value).isdigit() and int(value) > 0 for value in identity)):
                dependencies.add(tuple(str(value) for value in identity))
    if selected_project is not None:
        dependencies.add((selected_project.board_id, selected_project.item_id))
    if selected_drawing_record is not None:
        dependencies.add((selected_drawing_record.board_id, selected_drawing_record.item_id))
    selected_drawing = None
    drawing_selection = None
    project = None
    baseline_issue = None
    baseline_sources = None
    source_issues = [SourceIssue(source_id=source["sourceId"], code=source["status"],
        detail=f"{source['filename']}: {SOURCE_REVIEW_DETAILS[source['status']]}")
        for source in extraction["sourceManifest"] if source.get("status") in SOURCE_REVIEW_DETAILS]

    def record_error(label, detail, source_item=None, column_id=None):
        crm_errors.setdefault(label, detail)
        crm_reads.append({"field": label, "status": "unavailable", "reason": detail,
            **({"boardId": str(source_item["board"]["id"]), "itemId": str(source_item["id"]),
                "columnId": column_id} if source_item else {})})

    def read_crm(source_item, scope, label):
        try:
            column = _find_column(source_item, label, scope)
            if column is None and label == "Company" and scope == "project":
                columns = [entry for entry in _columns(source_item)
                    if _normal((entry.get("column") or {}).get("title")) in {"account", "accounts"}]
                if len(columns) > 1:
                    raise SpecificationSourceError("Several project Account columns require an explicit mapping")
                column = columns[0] if columns else None
            if column is None:
                return
            value = _read_column(column, dependencies, gateway)
            evidence = CRMFieldRead(record={"board_id": str(source_item["board"]["id"]),
                "item_id": str(source_item["id"])}, column_id=column["id"])
            crm_reads.append({"source": "Monday CRM", "boardId": evidence.record.board_id,
                "itemId": evidence.record.item_id, "columnId": evidence.column_id,
                "field": label, "value": value, "status": "empty" if value is None else "provided",
                "columnValue": {key: value for key, value in column.items() if key not in {"value", "display_value"}}})
            if value is not None:
                # A selected drawing is more specific than a project fallback.
                crm_values[label] = CRMRecommendation(value=value, evidence=[evidence],
                    rationale="Selected drawing recommendation" if scope == "drawing" else "Selected project recommendation")
                crm_errors.pop(label, None)
        except (SpecificationSourceError, SpecificationValueConflict) as exc:
            detail = str(exc)
            if isinstance(exc, SpecificationValueConflict) and exc.values:
                detail += ". Reported values: " + "; ".join(str(value) for value in exc.values)
            record_error(label, detail, source_item, column["id"] if "column" in locals() and column else None)
            if "column" in locals() and column:
                crm_reads[-1]["columnValue"] = {
                    key: value for key, value in column.items() if key not in {"value", "display_value"}}

    if request_type == "amendment" and selected_project:
        dependencies.add((selected_project.board_id, selected_project.item_id))
        try:
            project = gateway.fetch_project(selected_project.item_id)
            if (str(project.get("id")) != selected_project.item_id
                    or str((project.get("board") or {}).get("id")) != selected_project.board_id
                    or project.get("state") != "active" or not isinstance(project.get("subitems"), list)):
                raise SpecificationSourceError("Selected project is incomplete or inactive")
        except Exception:
            project = None
            candidate_lookup.status = "unavailable"
            candidate_lookup.drawings = [candidate for candidate in candidate_lookup.drawings
                if candidate.project != selected_project]
            candidate_lookup.issues.append("Selected project recommendations are unavailable; retry the lookup")
            for label in FIELD_NAMES:
                if "monday_crm" in FIELD_POLICY[label].amendment:
                    record_error(label, "Selected project recommendations are unavailable")

    if project is not None:
        matched_project = projects.get(selected_project.item_id)
        if matched_project is not None:
            for label, value in _project_display_labels(project=project).items():
                if not getattr(matched_project, label) and value:
                    setattr(matched_project, label, value)
        requested = email_raw.get("Drawing Reference")
        baseline_evidence = []
        if not requested:
            baseline_evidence, baseline_issue = resolve_baseline_drawing_identity(extraction)
            if baseline_issue:
                _, baseline_sources, _ = resolve_email_evidence(extraction, _include_baseline_references=True)
            if baseline_evidence and not baseline_issue:
                requested = baseline_evidence[0]["value"]
        ranked = recommend_drawing_candidates(project["subitems"], requested_reference=requested,
            project_reference=project.get("name"), scope=email_raw.get("Bauder Roof Num"))
        drawing_selection = {**ranked, "reference": requested,
            **({"evidence": baseline_evidence} if baseline_evidence else {})}
        if baseline_issue or ("Drawing Reference" in email_issues and not requested):
            ranked["selectedItemId"] = None
        active = [drawing for drawing in project["subitems"] if drawing.get("state") == "active"]
        if selected_drawing_record is not None:
            selected_drawing = next((drawing for drawing in active
                if str(drawing.get("id")) == selected_drawing_record.item_id
                and str((drawing.get("board") or {}).get("id")) == selected_drawing_record.board_id), None)
            if selected_drawing is None:
                record_error("Drawing Reference", "Selected drawing is unavailable, inactive or belongs to another project")
        else:
            selected_drawing = next((drawing for drawing in active
                if str(drawing.get("id")) == ranked["selectedItemId"]), None)
            if selected_drawing:
                selected_drawing_record = MondayRecord(board_id=str(selected_drawing["board"]["id"]),
                    item_id=str(selected_drawing["id"]))
        candidate_lookup.drawings = []
        for candidate in ranked["candidates"]:
            if not candidate["boardId"] or not candidate["itemId"]:
                continue
            record = MondayRecord(board_id=candidate["boardId"], item_id=candidate["itemId"])
            if state.selections.selected_drawing is None or record == state.selections.selected_drawing:
                dependencies.add((record.board_id, record.item_id))
            candidate_lookup.drawings.append(DrawingCandidate(record=record, project=selected_project,
                literal_reference=candidate["reference"] or "Unreadable reference", roof_scope=candidate["scope"],
                lifecycle=candidate["state"] if candidate["state"] in {"active", "archived", "deleted"} else "unknown",
                match_kind="exact_email" if ranked["status"] == "exact" and candidate["itemId"] == ranked["selectedItemId"]
                    else "latest_in_scope" if any(entry["itemId"] == candidate["itemId"] for entry in ranked["recommendations"])
                    else "other", rationale="Existing drawing candidate; confirm project and roof scope"))
        candidate_lookup.issues.extend(ranked["issues"])
        if selected_drawing:
            # Narrow attachment evidence before choosing fallback fields. No
            # technical value is merged from another roof's candidate drawing.
            email_raw, email_sources, email_issues = resolve_email_evidence(extraction,
                (email_raw.get("Bauder Roof Num"), selected_drawing.get("name")))
            if email_issues.get("Drawing Reference") == UNCONFIRMED_REFERENCE:
                email_issues.pop("Drawing Reference")
            name = str(selected_drawing.get("name") or "")
            if tp_reference_revision(name, confirmed_crm=True):
                crm_values["Drawing Reference"] = CRMRecommendation(value=name,
                    evidence=[CRMFieldRead(record=selected_drawing_record, column_id="name")],
                    rationale="Selected existing drawing; no future revision is generated")
            read_crm(selected_drawing, "drawing", "Revision")
            for label in FIELD_NAMES:
                if "monday_crm" not in FIELD_POLICY[label].amendment or label in {"Company", "Drawing Reference"}:
                    continue
                # A valid populated email fact makes a CRM technical read an
                # unrelated input. Keep its publication/cache identity stable.
                if not missing(email_raw.get(label)) or label in email_issues:
                    continue
                read_crm(project, "project", label)
                read_crm(selected_drawing, "drawing", label)
        else:
            crm_errors["Drawing Reference"] = UNMATCHED_DRAWING if requested else UNASSIGNED_DRAWING
            if baseline_evidence and not baseline_issue:
                # A verified baseline can identify a reference for review, but
                # its old technical facts are never promoted into the request.
                email_raw["Drawing Reference"] = requested
                email_sources["Drawing Reference"] = {"source": "Email/Attachment", "status": "provided",
                    "candidates": baseline_evidence, "selected": baseline_evidence[0],
                    "rationale": "Existing drawing identity; confirm the appropriate active drawing"}
        read_crm(project, "project", "Company")

    fields = {}
    automatic_sources = {}
    for label in FIELD_NAMES:
        raw = email_raw.get(label)
        provenance = deepcopy(email_sources[label])
        email_evidence = _citations(provenance)
        proposal_source, value, evidence, rationale = "unavailable", None, {}, "No applicable value has been provided"
        issues = []
        if label in email_issues:
            issues.append(FieldIssue(code="email_evidence_review", detail=email_issues[label]))
        if label == "Drawing Reference" and baseline_issue:
            issues.append(FieldIssue(code="baseline_identity_review", detail=baseline_issue))
        if label in crm_errors:
            issues.append(FieldIssue(code="crm_recommendation_unavailable", detail=crm_errors[label]))
        if label == "Incoming Information":
            proposal_source, value, rationale = "default_blank", "", "Blank by default; a reviewer may specify this field"
            issues = []
        elif request_type == "new_enquiry" and label in {"Drawing Reference", "Revision"}:
            proposal_source, rationale = "unassigned", "Drawing identity is assigned after design; a reviewer may specify it"
            issues = []
        elif label == "Revision":
            # Filled from the resolved reference below, including its full suffix.
            pass
        elif (request_type == "amendment" and label == "Drawing Reference" and selected_drawing
                and "Drawing Reference" in crm_values and not missing(raw)
                and not drawing_references_equal(raw, selected_drawing.get("name"),
                    confirmed_project_reference=project.get("name"))):
            recommendation = crm_values[label]
            proposal_source, value, rationale = "monday_crm", recommendation.value, recommendation.rationale
            evidence = {"crm_evidence": recommendation.evidence}
            issues.append(FieldIssue(code="drawing_reference_disagreement",
                detail="Selected existing drawing differs from the email reference; confirm the drawing recommendation"))
        elif request_type == "amendment" and label == "Company":
            recommendation = crm_values.get(label)
            if recommendation:
                proposal_source, value, rationale = "monday_crm", recommendation.value, recommendation.rationale
                evidence = {"crm_evidence": recommendation.evidence}
                if not missing(raw) and _normal(raw) != _normal(value):
                    issues.append(FieldIssue(code="company_disagreement",
                        detail=f"Email company '{raw}' differs from the selected project's Account '{value}'; reconcile the recommendation"))
            elif not issues:
                issues.append(FieldIssue(code="project_account_required", detail="Select a project with a readable Account recommendation"))
        elif not missing(raw) and email_evidence:
            proposal_source, value, rationale = "email_context", raw, provenance.get("rationale", "Current client request")
            selected = provenance.get("selected")
            evidence = {"email_evidence": [EmailCitation(source_id=selected["source_id"], excerpt=selected["snippet"],
                location=selected["location"], scope=selected.get("scope"))] if selected else email_evidence}
        elif request_type == "amendment" and label not in email_issues and label in crm_values:
            recommendation = crm_values[label]
            proposal_source, value, rationale = "monday_crm", recommendation.value, recommendation.rationale
            evidence = {"crm_evidence": recommendation.evidence}
        mapping = (CADMapping(status="not_applicable")
            if proposal_source in {"unassigned", "default_blank", "unavailable"}
            else reviewer_cad_mapping(label, value))
        proposal = AutomaticProposal(proposal_id=review_digest({"label": label, "source": proposal_source,
            "value": value, "evidence": {key: [entry.model_dump(mode="json") for entry in entries] for key, entries in evidence.items()}}),
            display_value=value, source=proposal_source, rationale=rationale, cad_mapping=mapping, **evidence)
        fields[label] = ReviewFieldSources(email_facts=_email_facts(provenance),
            crm_recommendations=[crm_values[label]] if label in crm_values else [], proposal=proposal,
            applicability="unassigned" if proposal_source == "unassigned" else "not_applicable" if proposal_source == "default_blank"
                else "unresolved" if proposal_source == "unavailable" or issues else "applicable", issues=issues)
        automatic_sources[label] = provenance

    if request_type == "amendment":
        reference = fields["Drawing Reference"].proposal
        confirmed = reference.source == "monday_crm" or bool(selected_drawing and drawing_references_equal(
            reference.display_value, selected_drawing.get("name"), confirmed_project_reference=project.get("name")))
        suffix = tp_reference_revision(reference.display_value, confirmed_crm=confirmed)
        if suffix:
            fields["Revision"].proposal = AutomaticProposal(proposal_id=review_digest({"reference": reference.proposal_id, "suffix": suffix}),
                display_value=suffix, source="derived", rationale="Full suffix of the selected existing Drawing Reference",
                email_evidence=reference.email_evidence, crm_evidence=reference.crm_evidence, derived_from="Drawing Reference",
                cad_mapping=reviewer_cad_mapping("Revision", suffix))
            fields["Revision"].applicability = "applicable"
            observed = email_raw.get("Revision")
            if observed and observed.casefold() not in {suffix.casefold(), suffix.rsplit("-", 1)[-1].strip().casefold()}:
                fields["Revision"].issues.append(FieldIssue(code="revision_disagreement",
                    detail="Revision conflicts with the selected existing drawing reference"))
            crm_revision = crm_values.get("Revision")
            if crm_revision and crm_revision.value.casefold() not in {suffix.casefold(), suffix.rsplit("-", 1)[-1].strip().casefold()}:
                fields["Revision"].issues.append(FieldIssue(code="crm_revision_disagreement",
                    detail="CRM Revision conflicts with the selected existing drawing reference"))
        elif reference.display_value:
            fields["Drawing Reference"].issues.append(FieldIssue(code="drawing_reference_unverified",
                detail="Reference is not a verified existing drawing"))

    # Source identities contain policy-relevant facts, never actor/polling times,
    # holding business columns, approval events or arbitrary CRM columns.
    extraction_identity = review_digest({"input_revision": target.input_revision,
        "pipeline": extraction["pipelineVersion"], "evidence_version": extraction["specificationEvidenceVersion"],
        "reference_parser_version": extraction.get("referenceParserVersion")})
    source_payload = {"extraction_identity": extraction_identity, "request_type": request_type,
        "intent": intent.model_dump(mode="json"), "selected_project": selected_project.model_dump(mode="json") if selected_project else None,
        "selected_drawing": selected_drawing_record.model_dump(mode="json") if selected_drawing_record else None,
        "fields": {label: value.model_dump(mode="json") for label, value in fields.items()},
        "candidates": candidate_lookup.model_dump(mode="json"),
        "source_issues": [issue.model_dump(mode="json") for issue in source_issues],
        "input_sources": [{key: source.get(key) for key in (
            "sourceId", "sourceType", "filename", "contentSha256", "status", "parentSourceId")}
            for source in extraction["sourceManifest"]],
        "request_notes": request_notes,
        "evidence_repairs": extraction.get("specificationEvidenceRepairs", []),
        "source_policy_version": SOURCE_POLICY_VERSION, "reference_parser_version": REFERENCE_PARSER_VERSION}
    identity = SourceIdentity(source_fingerprint=review_digest(source_payload), extraction_identity=extraction_identity,
        source_policy_version=SOURCE_POLICY_VERSION, reference_parser_version=REFERENCE_PARSER_VERSION)
    snapshot = ReviewSourceSnapshot(identity=identity, intent=intent, request_type=request_type,
        selected_project=selected_project, selected_drawing=selected_drawing_record, candidates=candidate_lookup,
        fields=fields, source_issues=source_issues)
    effective_raw, effective_sources, effective_issues = {}, {}, {}
    for label in FIELD_NAMES:
        sources = fields[label]
        saved = state.field_decisions.get(label)
        effective = effective_field_value(sources.proposal, saved, current_source=identity)
        effective_raw[label] = effective.display_value
        proposal = saved.decision.snapshot if saved and saved.decision.operation == "accept_recommendation" else sources.proposal
        effective_sources[label] = {**automatic_sources[label], "source": effective.source_label,
            "status": "provided" if effective.display_value else "blank" if effective.display_value == "" else "missing",
            "rationale": proposal.rationale}
        if proposal.crm_evidence and effective.source != "reviewer_specified":
            read = proposal.crm_evidence[0]
            effective_sources[label].update(boardId=read.record.board_id, itemId=read.record.item_id, columnId=read.column_id)
            column_read = next((entry for entry in reversed(crm_reads) if entry.get("field") == label
                and entry.get("itemId") == read.record.item_id and entry.get("columnId") == read.column_id), None)
            if column_read and "columnValue" in column_read:
                effective_sources[label]["columnValue"] = column_read["columnValue"]
        if label == "Revision" and effective.source == "derived":
            effective_sources[label].update(source="Derived", **{"from": "Drawing Reference"},
                referenceSource=effective_sources.get("Drawing Reference"),
                observedRevision=crm_values["Revision"].value if "Revision" in crm_values else email_raw.get("Revision"))
        if sources.issues and saved is None:
            effective_issues[label] = "; ".join(issue.detail for issue in sources.issues)
    result = {"raw": effective_raw, "sources": effective_sources, "issues": effective_issues,
        "crmReads": crm_reads, "sourceManifest": extraction["sourceManifest"],
        "requestContext": request_context_summary(select_client_request(extraction["sourceManifest"])),
        "requestNotes": request_notes, "evidenceRepairs": extraction.get("specificationEvidenceRepairs", []),
        "evidenceVersion": extraction["specificationEvidenceVersion"],
        "dependencies": [list(value) for value in sorted(dependencies)],
        "emailRevision": target.input_revision, "extractionPipeline": extraction["pipelineVersion"],
        "referenceParserVersion": REFERENCE_PARSER_VERSION, "sourcePolicyVersion": SOURCE_POLICY_VERSION,
        "scopePolicy": SPECIFICATION_SCOPE_POLICY_VERSION,
        "requestIntent": intent.model_dump(mode="json"), "reviewSource": snapshot.model_dump(mode="json"),
        "drawingItemId": selected_drawing_record.item_id if selected_drawing_record else None,
        **({"drawingSelection": drawing_selection} if drawing_selection else {})}
    publishable_email_issues = {VALUE_CONFLICT, UNDATED_SCOPE_CONFLICT, PAGE_SIZE_CONFLICT, PAGE_SIZE_UNCERTAIN}
    unverified = {label: issue for label, issue in email_issues.items()
        if label not in state.field_decisions and fields[label].proposal.source not in {"unassigned", "default_blank"}
        and (issue not in publishable_email_issues
            or any(entry.get("status") == "unverified" for entry in email_sources[label].get("rejectedCandidates", [])))}
    if baseline_issue and "Drawing Reference" not in state.field_decisions:
        unverified["Drawing Reference"] = baseline_issue
        if baseline_sources:
            result["sources"]["Drawing Reference"] = baseline_sources["Drawing Reference"]
    if unverified:
        result["issues"].update(unverified)
        raise SpecificationReviewRequired(result, result["issues"])
    return result
