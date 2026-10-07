"""Closed vocabularies for workflow, requirement, review, and evidence state."""

from enum import StrEnum


class WorkflowType(StrEnum):
    GRANT = "grant"
    COMPLIANCE = "compliance"
    VENDOR_CONTRACT = "vendor_contract"
    CUSTOMER_IMPLEMENTATION = "customer_implementation"
    REGULATORY_REPORTING = "regulatory_reporting"
    GENERAL = "general"


class WorkflowStatus(StrEnum):
    DRAFT = "draft"
    ACTIVE = "active"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class RequirementProvenance(StrEnum):
    ORGANIZATION_TEMPLATE = "organization_template"
    DOCUMENT_EXPLICIT = "document_explicit"
    AI_INFERRED = "ai_inferred"
    USER_PROVIDED = "user_provided"
    SYSTEM_DERIVED = "system_derived"


class ReviewStatus(StrEnum):
    PENDING_REVIEW = "pending_review"
    APPROVED = "approved"
    REJECTED = "rejected"


class RequirementStatus(StrEnum):
    NOT_STARTED = "not_started"
    IN_PROGRESS = "in_progress"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class EvidenceType(StrEnum):
    DOCUMENT = "document"
    LINK = "link"
    NOTE = "note"
    RECORD = "record"


class EvidenceStatus(StrEnum):
    SUBMITTED = "submitted"
