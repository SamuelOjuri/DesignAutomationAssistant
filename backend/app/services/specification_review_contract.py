"""Typed source, decision and display contracts for Specification Review.

These are application contracts, separate from the strict automated CAD schema.
They describe trusted source snapshots and server-authored decisions. Untrusted
save commands cannot supply actor identities, provenance or accepted values.
No model, Monday, storage or database operations take place in this module.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from .specification_semantics import FIELD_DEFINITIONS, PDF_FIELD_NAMES as FIELD_NAMES, SpecificationField


REVIEW_SCHEMA_VERSION = 1
REQUEST_INTENT_SCHEMA_VERSION = 1
SOURCE_POLICY_VERSION = "specification-review-source-v1"

RequestType = Literal["new_enquiry", "amendment"]
RecommendedRequestType = Literal["new_enquiry", "amendment", "uncertain"]
AutomaticSource = Literal[
    "email_context", "monday_crm", "derived", "unassigned", "default_blank", "unavailable",
]
FieldSource = Literal[
    "email_context", "monday_crm", "derived", "unassigned", "default_blank", "unavailable",
    "reviewer_specified",
]
NonemptyText = Annotated[str, Field(min_length=1, pattern=r"\S")]
DisplayText = Annotated[str, Field(min_length=1, max_length=8192, pattern=r"\S")]
MondayId = Annotated[str, Field(pattern=r"^[1-9][0-9]*$")]

SOURCE_LABELS = MappingProxyType({
    "email_context": "Email Context",
    "monday_crm": "Monday CRM recommendation",
    "derived": "Selected Drawing Reference",
    "unassigned": "Not yet assigned",
    "default_blank": "Default blank",
    "unavailable": "Not Provided",
    "reviewer_specified": "Reviewer Specified",
})

# Keep the current extraction/CAD descriptions unchanged until their producers
# adopt the review policy. The reviewer contract has its own field meanings.
REVIEW_FIELD_DEFINITIONS = MappingProxyType({
    label: {
        "Drawing Reference": (
            "The issued project drawing reference, normally two alphabetic prefix characters, "
            "project digits, underscore and numeric drawing/version suffix (e.g. AB12345_26.01 - A). "
            "Preserve the actual prefix and leading zeros. Use the relevant existing drawing; "
            "offer controlled existing candidates when email has no suitable target."
        ),
        "Revision": (
            "The complete suffix after the underscore in the selected existing Drawing Reference "
            "(e.g. 26.01 - AA). Never invent a future issued revision. A reviewer value takes precedence."
        ),
        "Incoming Information": "Blank by default; a reviewer may specify text or an intentional blank.",
    }.get(label, FIELD_DEFINITIONS[label]) for label in FIELD_NAMES
})


@dataclass(frozen=True)
class FieldPolicy:
    """Ordered automatic sources; a reviewer may override every field.

    A valid absent higher-priority fact permits fallback. An unavailable,
    conflicting or ambiguously scoped source remains visible for review.
    """

    new_enquiry: tuple[AutomaticSource, ...]
    amendment: tuple[AutomaticSource, ...]
    rationale: str
    unassigned_permitted: bool = False

    def sources_for(self, request_type: RequestType) -> tuple[AutomaticSource, ...]:
        if request_type == "new_enquiry":
            return self.new_enquiry
        if request_type == "amendment":
            return self.amendment
        raise ValueError("An uncertain request requires reviewer classification")


_EMAIL_ONLY = ("email_context",)
_EMAIL_THEN_CRM = ("email_context", "monday_crm")
FIELD_POLICY = MappingProxyType({
    "Scale": FieldPolicy(_EMAIL_ONLY, _EMAIL_ONLY, "Relevant email/attachment scale."),
    "Page Size": FieldPolicy(_EMAIL_ONLY, _EMAIL_ONLY, "Relevant drawing/title-block evidence."),
    "Drawing Reference": FieldPolicy(("unassigned",), _EMAIL_THEN_CRM,
        "New drawing identity is unassigned; amendment prefers a relevant email target, then a scoped existing drawing.", True),
    "Drawing Title": FieldPolicy(_EMAIL_ONLY, _EMAIL_ONLY,
        "Keep the request title; the selected project title is context."),
    "Bauder Contract Number": FieldPolicy(_EMAIL_ONLY, _EMAIL_ONLY, "Explicit email contract reference."),
    "Bauder Roof Num": FieldPolicy(_EMAIL_ONLY, _EMAIL_ONLY, "Explicit email/attachment roof identifier scopes candidates."),
    "Incoming Information": FieldPolicy(("default_blank",), ("default_blank",), "Automatic blank; reviewer may supply a value."),
    "Revision": FieldPolicy(("unassigned",), ("derived",),
        "Full suffix of the selected existing reference; never a future revision.", True),
    "Date Received": FieldPolicy(_EMAIL_ONLY, _EMAIL_ONLY,
        "Selected client request date; ignore holding-item and old-project dates."),
    "Company": FieldPolicy(_EMAIL_ONLY, ("monday_crm",),
        "New enquiry uses email requester with optional canonical directory identity. Amendment recommends the project's Account and exposes differing email company."),
    "Contact": FieldPolicy(_EMAIL_ONLY, _EMAIL_THEN_CRM,
        "Current email requester first; appropriate selected drawing/request Contact fallback, not an aggregate mirror."),
    "Surveyor": FieldPolicy(_EMAIL_ONLY, _EMAIL_THEN_CRM, "Email then selected project/drawing recommendation."),
    "Target U Value": FieldPolicy(_EMAIL_ONLY, _EMAIL_THEN_CRM, "Requested email target then scoped CRM fallback."),
    "Target Min U Value": FieldPolicy(_EMAIL_ONLY, _EMAIL_THEN_CRM, "Explicit minimum/secondary email target then scoped CRM fallback."),
    "Membrane": FieldPolicy(_EMAIL_ONLY, _EMAIL_THEN_CRM, "Email then scoped CRM fallback."),
    "Tapered Insulation": FieldPolicy(_EMAIL_ONLY, _EMAIL_THEN_CRM, "Email then scoped CRM fallback."),
    "VCL": FieldPolicy(_EMAIL_ONLY, _EMAIL_THEN_CRM, "Email then scoped CRM fallback."),
    "Decking": FieldPolicy(_EMAIL_ONLY, _EMAIL_THEN_CRM, "Applicable roof evidence then scoped CRM fallback."),
})


class ReviewModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_default=True)


class MondayRecord(ReviewModel):
    """Monday identities are never printed project/drawing reference numbers."""

    board_id: MondayId
    item_id: MondayId


class EmailCitation(ReviewModel):
    source_id: NonemptyText
    excerpt: Annotated[str, Field(min_length=1, max_length=4000, pattern=r"\S")]
    location: NonemptyText
    scope: str | None = None


class CRMFieldRead(ReviewModel):
    record: MondayRecord
    column_id: NonemptyText
    scope: str | None = None


class DetectedReference(ReviewModel):
    """Literal reference and source role; parsed identity never replaces evidence."""

    kind: Literal["project", "drawing"]
    literal: NonemptyText
    evidence: EmailCitation
    role: Literal["current_request", "forwarded_context", "document"] = "current_request"


class ProjectMatch(ReviewModel):
    record: MondayRecord
    rationale: NonemptyText
    # A matcher score is supporting evidence, never a calibrated intent probability.
    similarity_score: float | None = Field(default=None, ge=0, le=1)


class DrawingCandidate(ReviewModel):
    record: MondayRecord
    project: MondayRecord
    literal_reference: NonemptyText
    roof_scope: str | None = None
    lifecycle: Literal["active", "archived", "deleted", "unknown"]
    match_kind: Literal["exact_email", "latest_in_scope", "other"]
    rationale: NonemptyText


class CandidateLookup(ReviewModel):
    """A successful empty lookup is distinct from a failed/unavailable read."""

    status: Literal["available", "unavailable", "pending"]
    projects: list[ProjectMatch] = Field(default_factory=list)
    drawings: list[DrawingCandidate] = Field(default_factory=list)
    issues: list[NonemptyText] = Field(default_factory=list)


class RequestIntentRecommendation(ReviewModel):
    schema_version: Literal[1] = REQUEST_INTENT_SCHEMA_VERSION
    recommended_type: RecommendedRequestType
    explanation: NonemptyText
    evidence: list[EmailCitation] = Field(default_factory=list)
    detected_references: list[DetectedReference] = Field(default_factory=list)
    matched_projects: list[ProjectMatch] = Field(default_factory=list)
    ambiguity_reasons: list[NonemptyText] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_intent_evidence(self):
        if self.recommended_type != "uncertain" and not self.evidence:
            raise ValueError("A request-type recommendation requires supporting email context, not just a project match")
        return self


class SourceIdentity(ReviewModel):
    """Versions must describe actual producers; old extraction cannot be relabelled."""

    source_fingerprint: NonemptyText
    extraction_identity: NonemptyText
    source_policy_version: NonemptyText
    reference_parser_version: NonemptyText


class CADMapping(ReviewModel):
    """Server-validated mapping, independent from literal PDF display text.

    A mapped value requires its authoritative catalogue/contract version. This
    schema does not establish that a catalogue entry or TRICS identity exists.
    """

    status: Literal["mapped", "unmapped", "not_applicable"] = "unmapped"
    value: DisplayText | None = None
    catalogue_version: NonemptyText | None = None
    target_identity: NonemptyText | None = None

    @model_validator(mode="after")
    def check_mapping(self):
        if self.status == "mapped":
            if self.value is None or self.catalogue_version is None:
                raise ValueError("A mapped CAD value requires a value and authoritative catalogue/contract version")
        elif any(value is not None for value in (self.value, self.catalogue_version, self.target_identity)):
            raise ValueError("An unmapped/not-applicable value cannot claim a CAD value or identity")
        return self


class FieldIssue(ReviewModel):
    code: NonemptyText
    detail: NonemptyText


class SourceIssue(FieldIssue):
    source_id: NonemptyText


class EmailFact(ReviewModel):
    value: DisplayText
    evidence: list[EmailCitation] = Field(min_length=1)


class CRMRecommendation(ReviewModel):
    value: DisplayText
    evidence: list[CRMFieldRead] = Field(min_length=1)
    rationale: NonemptyText


class AutomaticProposal(ReviewModel):
    """A resolved proposal, not the raw email/CRM candidates it was based on."""

    proposal_id: NonemptyText
    display_value: str | None = Field(max_length=8192)
    source: AutomaticSource
    rationale: NonemptyText
    email_evidence: list[EmailCitation] = Field(default_factory=list)
    crm_evidence: list[CRMFieldRead] = Field(default_factory=list)
    derived_from: Literal["Drawing Reference"] | None = None
    cad_mapping: CADMapping = Field(default_factory=CADMapping)

    @model_validator(mode="after")
    def check_provenance(self):
        if self.source in {"unassigned", "default_blank", "unavailable"}:
            expected = "" if self.source == "default_blank" else None
            if self.display_value != expected:
                raise ValueError("Missing/default/unassigned proposals must preserve their distinct blank semantics")
            if self.email_evidence or self.crm_evidence or self.derived_from:
                raise ValueError("A missing/default/unassigned proposal cannot claim field provenance")
            if self.cad_mapping.status != "not_applicable":
                raise ValueError("A missing/default/unassigned proposal has no CAD mapping")
        else:
            if self.display_value is None or not self.display_value.strip():
                raise ValueError("A populated proposal requires a nonblank value")
            if self.source == "email_context" and not self.email_evidence:
                raise ValueError("Email Context requires supporting email evidence")
            if self.source == "monday_crm" and not self.crm_evidence:
                raise ValueError("Monday CRM requires exact record/column evidence")
            if self.source == "derived" and (self.derived_from is None or not (self.email_evidence or self.crm_evidence)):
                raise ValueError("A derived Revision requires its selected Drawing Reference provenance")
            if self.source != "derived" and self.derived_from is not None:
                raise ValueError("Only derived values can declare a derivation")
        return self


class ReviewActor(ReviewModel):
    app_user_id: NonemptyText
    monday_user_id: MondayId
    monday_account_id: MondayId


class DecisionAudit(ReviewModel):
    """Authored by the server, never accepted from a decisions command."""

    actor: ReviewActor
    saved_at: AwareDatetime
    reviewed_source: SourceIdentity
    explanation: str | None = Field(default=None, max_length=4000)


class AcceptedRecommendation(ReviewModel):
    operation: Literal["accept_recommendation"]
    snapshot: AutomaticProposal

    @model_validator(mode="after")
    def check_acceptable(self):
        if self.snapshot.source == "unavailable":
            raise ValueError("An unavailable recommendation cannot be accepted")
        return self


class SpecifiedValue(ReviewModel):
    operation: Literal["specify_value"]
    display_value: DisplayText
    cad_mapping: CADMapping = Field(default_factory=CADMapping)


class SpecifiedBlank(ReviewModel):
    operation: Literal["specify_blank"]


SavedDecisionValue = Annotated[
    AcceptedRecommendation | SpecifiedValue | SpecifiedBlank, Field(discriminator="operation"),
]


class SavedFieldDecision(ReviewModel):
    decision: SavedDecisionValue
    audit: DecisionAudit


class AcceptRecommendationCommand(ReviewModel):
    operation: Literal["accept_recommendation"]
    proposal_id: NonemptyText


class SpecifyValueCommand(ReviewModel):
    operation: Literal["specify_value"]
    display_value: DisplayText


class SpecifyBlankCommand(ReviewModel):
    operation: Literal["specify_blank"]


class RestoreRecommendationCommand(ReviewModel):
    operation: Literal["restore_recommendation"]


FieldCommand = Annotated[
    AcceptRecommendationCommand | SpecifyValueCommand | SpecifyBlankCommand | RestoreRecommendationCommand,
    Field(discriminator="operation"),
]


class SourceDispositionCommand(ReviewModel):
    source_id: NonemptyText
    issue_code: NonemptyText
    explanation: NonemptyText


class SourceDisposition(SourceDispositionCommand):
    audit: DecisionAudit


class SelectionChanges(ReviewModel):
    selected_request_type: RequestType | None = None
    selected_project: MondayRecord | None = None
    selected_drawing: MondayRecord | None = None


class ReviewSelections(SelectionChanges):
    @model_validator(mode="after")
    def check_drawing_selection(self):
        if self.selected_drawing is not None and self.selected_project is None:
            raise ValueError("A drawing selection requires a project selection")
        return self


class ReviewDecisionState(ReviewModel):
    schema_version: Literal[1] = REVIEW_SCHEMA_VERSION
    review_version: int = Field(default=0, ge=0, strict=True)
    selections: ReviewSelections = Field(default_factory=ReviewSelections)
    # Absence means use the current recommendation. Restore is a command only.
    field_decisions: dict[SpecificationField, SavedFieldDecision] = Field(default_factory=dict)
    source_dispositions: list[SourceDisposition] = Field(default_factory=list)


class SaveReviewDecisions(ReviewModel):
    """Command boundary only; CAS/idempotency/authorization are Phase 2/6 work.

    Omitted selection members mean unchanged; explicit null clears a selection.
    The service must consult selections.model_fields_set when applying a patch.
    """

    schema_version: Literal[1] = REVIEW_SCHEMA_VERSION
    expected_review_version: int = Field(ge=0, strict=True)
    expected_source_fingerprint: NonemptyText
    idempotency_key: Annotated[str, Field(min_length=1, max_length=128, pattern=r"\S")]
    selections: SelectionChanges | None = None
    field_operations: dict[SpecificationField, FieldCommand] = Field(default_factory=dict)
    source_dispositions: list[SourceDispositionCommand] = Field(default_factory=list)
    explanation: str | None = Field(default=None, max_length=4000)


class EffectiveFieldValue(ReviewModel):
    display_value: str | None = Field(max_length=8192)
    source: FieldSource
    cad_mapping: CADMapping
    reviewer_accepted: bool = False
    requires_reconfirmation: bool = False

    @property
    def source_label(self) -> str:
        return SOURCE_LABELS[self.source]


def effective_field_value(
    proposal: AutomaticProposal,
    saved: SavedFieldDecision | None = None,
    *,
    current_source: SourceIdentity,
) -> EffectiveFieldValue:
    """Contract semantics only: preserve decisions without reading/resolving sources.

    A restored/absent decision uses the supplied current proposal. An accepted
    recommendation uses its saved snapshot even after recommendations change.
    Source changes flag reconfirmation while keeping the saved display intact.
    """
    changed = saved is not None and saved.audit.reviewed_source != current_source
    decision = saved.decision if saved is not None else None
    if isinstance(decision, SpecifiedValue):
        return EffectiveFieldValue(display_value=decision.display_value, source="reviewer_specified",
            cad_mapping=decision.cad_mapping.model_copy(deep=True), requires_reconfirmation=changed)
    if isinstance(decision, SpecifiedBlank):
        return EffectiveFieldValue(display_value="", source="reviewer_specified",
            cad_mapping=CADMapping(status="not_applicable"), requires_reconfirmation=changed)
    accepted = isinstance(decision, AcceptedRecommendation)
    selected = decision.snapshot if accepted else proposal
    return EffectiveFieldValue(display_value=selected.display_value, source=selected.source,
        cad_mapping=selected.cad_mapping.model_copy(deep=True), reviewer_accepted=accepted,
        requires_reconfirmation=changed)


class ReviewFieldSources(ReviewModel):
    email_facts: list[EmailFact] = Field(default_factory=list)
    crm_recommendations: list[CRMRecommendation] = Field(default_factory=list)
    proposal: AutomaticProposal
    applicability: Literal["applicable", "unassigned", "not_applicable", "unresolved"] = "unresolved"
    scope: str | None = None
    issues: list[FieldIssue] = Field(default_factory=list)


class ReviewSourceSnapshot(ReviewModel):
    schema_version: Literal[1] = REVIEW_SCHEMA_VERSION
    identity: SourceIdentity
    intent: RequestIntentRecommendation
    # Effective recommendation branch, independent of the model's intent proposal.
    request_type: RequestType | None = None
    # Bind resolved recommendations to their actual project/drawing context.
    # A candidate list alone does not establish which roof supplied the values.
    selected_project: MondayRecord | None = None
    selected_drawing: MondayRecord | None = None
    candidates: CandidateLookup = Field(default_factory=lambda: CandidateLookup(status="pending"))
    fields: dict[SpecificationField, ReviewFieldSources]
    # Field overrides never erase source-level unread-file/review problems.
    source_issues: list[SourceIssue] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_fields(self):
        if self.selected_drawing is not None and self.selected_project is None:
            raise ValueError("Resolved drawing sources require their project context")
        if set(self.fields) != set(FIELD_NAMES):
            raise ValueError("A review source snapshot requires all 18 exact PDF labels")
        for label, sources in self.fields.items():
            policy = FIELD_POLICY[label]
            allowed = policy.sources_for(self.request_type) if self.request_type else policy.new_enquiry + policy.amendment
            if sources.proposal.source != "unavailable" and sources.proposal.source not in allowed:
                raise ValueError(f"{label} proposal source is outside the field policy")
        return self
