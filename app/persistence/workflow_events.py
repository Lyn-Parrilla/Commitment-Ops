"""Derive factual workflow events from the audit snapshots of one save.

Events stay on the existing audit row. The workflow identifier remains the
entity so PostgreSQL row-level security continues to hide another
organization's history. Context keeps identifiers, states, and dates. It does
not copy names, addresses, review notes, or evidence text.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any

_WORKFLOW_STATUS_EVENTS = {
    ("draft", "active"): "workflow.activated",
    ("active", "completed"): "workflow.completed",
    ("draft", "cancelled"): "workflow.cancelled",
    ("active", "cancelled"): "workflow.cancelled",
}
_REVIEW_EVENTS = {
    ("pending_review", "approved"): "requirement.approved",
    ("pending_review", "rejected"): "requirement.rejected",
    ("rejected", "pending_review"): "requirement.resubmitted",
}
_REQUIREMENT_STATUS_EVENTS = {
    ("not_started", "in_progress"): "requirement.started",
    ("blocked", "in_progress"): "requirement.started",
    ("in_progress", "blocked"): "requirement.blocked",
    ("not_started", "completed"): "requirement.completed",
    ("in_progress", "completed"): "requirement.completed",
    ("not_started", "cancelled"): "requirement.cancelled",
    ("in_progress", "cancelled"): "requirement.cancelled",
    ("blocked", "cancelled"): "requirement.cancelled",
}


@dataclass(frozen=True)
class WorkflowAuditChange:
    event_type: str
    before_state: dict[str, Any] | None
    after_state: dict[str, Any]


def derive_workflow_audit_changes(
    before: dict[str, Any] | None,
    after: dict[str, Any],
) -> list[WorkflowAuditChange]:
    """Return the events produced by one successful workflow save.

    An unchanged snapshot produces no event. A snapshot difference that no
    named event explains produces ``workflow.updated``.
    """

    if before is None:
        return [
            WorkflowAuditChange(
                event_type="workflow.created",
                before_state=None,
                after_state=after,
            )
        ]

    changes: list[WorkflowAuditChange] = []
    accounted = deepcopy(after)
    workflow_event = _WORKFLOW_STATUS_EVENTS.get(
        (before["status"], after["status"])
    )
    if workflow_event is not None:
        changes.append(
            WorkflowAuditChange(
                event_type=workflow_event,
                before_state=before,
                after_state=after,
            )
        )
        accounted["status"] = before["status"]

    before_requirements = {
        requirement["id"]: requirement for requirement in before["requirements"]
    }
    kept_requirements: list[dict[str, Any]] = []
    for requirement in after["requirements"]:
        previous = before_requirements.get(requirement["id"])
        if previous is None:
            changes.append(
                WorkflowAuditChange(
                    event_type="requirement.added",
                    before_state=None,
                    after_state=_requirement_context(after, requirement),
                )
            )
            changes.extend(_new_evidence(after, requirement, previous=None))
            continue
        kept = deepcopy(requirement)
        changes.extend(
            _requirement_changes(after, previous, requirement, kept)
        )
        kept_requirements.append(kept)
    accounted["requirements"] = kept_requirements

    if _canonical(accounted) != _canonical(before):
        changes.append(
            WorkflowAuditChange(
                event_type="workflow.updated",
                before_state=before,
                after_state=after,
            )
        )
    return changes


def _requirement_changes(
    workflow: dict[str, Any],
    previous: dict[str, Any],
    current: dict[str, Any],
    accounted: dict[str, Any],
) -> list[WorkflowAuditChange]:
    changes: list[WorkflowAuditChange] = []
    review_event = _REVIEW_EVENTS.get(
        (previous["review_status"], current["review_status"])
    )
    if review_event is not None:
        changes.append(
            _field_change(
                review_event,
                workflow,
                current,
                field="review_status",
                before_value=previous["review_status"],
                after_value=current["review_status"],
            )
        )
        accounted["review_status"] = previous["review_status"]

    status_event = _REQUIREMENT_STATUS_EVENTS.get(
        (previous["status"], current["status"])
    )
    if status_event is not None:
        changes.append(
            _field_change(
                status_event,
                workflow,
                current,
                field="status",
                before_value=previous["status"],
                after_value=current["status"],
            )
        )
        accounted["status"] = previous["status"]

    if previous["owner_id"] != current["owner_id"]:
        changes.append(
            _field_change(
                "requirement.owner_changed",
                workflow,
                current,
                field="owner_id",
                before_value=previous["owner_id"],
                after_value=current["owner_id"],
            )
        )
        accounted["owner_id"] = previous["owner_id"]

    if previous["due_date"] != current["due_date"]:
        changes.append(
            _field_change(
                "requirement.due_date_changed",
                workflow,
                current,
                field="due_date",
                before_value=previous["due_date"],
                after_value=current["due_date"],
            )
        )
        accounted["due_date"] = previous["due_date"]

    changes.extend(_new_evidence(workflow, current, previous))
    previous_evidence = {
        evidence["id"] for evidence in previous["evidence"]
    }
    accounted["evidence"] = [
        evidence
        for evidence in current["evidence"]
        if evidence["id"] in previous_evidence
    ]
    return changes


def _new_evidence(
    workflow: dict[str, Any],
    requirement: dict[str, Any],
    previous: dict[str, Any] | None,
) -> list[WorkflowAuditChange]:
    previous_ids = set() if previous is None else {
        evidence["id"] for evidence in previous["evidence"]
    }
    return [
        WorkflowAuditChange(
            event_type="evidence.added",
            before_state=None,
            after_state={
                "workflow_id": workflow["id"],
                "organization_id": workflow["organization_id"],
                "requirement_id": requirement["id"],
                "evidence_id": evidence["id"],
                "status": evidence["status"],
            },
        )
        for evidence in requirement["evidence"]
        if evidence["id"] not in previous_ids
    ]


def _field_change(
    event_type: str,
    workflow: dict[str, Any],
    requirement: dict[str, Any],
    *,
    field: str,
    before_value: Any,
    after_value: Any,
) -> WorkflowAuditChange:
    return WorkflowAuditChange(
        event_type=event_type,
        before_state={
            "requirement_id": requirement["id"],
            field: before_value,
        },
        after_state={
            "workflow_id": workflow["id"],
            "organization_id": workflow["organization_id"],
            "requirement_id": requirement["id"],
            field: after_value,
        },
    )


def _requirement_context(
    workflow: dict[str, Any],
    requirement: dict[str, Any],
) -> dict[str, Any]:
    return {
        "workflow_id": workflow["id"],
        "organization_id": workflow["organization_id"],
        "requirement_id": requirement["id"],
        "status": requirement["status"],
        "review_status": requirement["review_status"],
        "owner_id": requirement["owner_id"],
        "due_date": requirement["due_date"],
    }


def _canonical(snapshot: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": snapshot["id"],
        "organization_id": snapshot["organization_id"],
        "workflow_type": snapshot["workflow_type"],
        "status": snapshot["status"],
        "requirements": {
            requirement["id"]: {
                "owner_id": requirement["owner_id"],
                "supervisor_id": requirement["supervisor_id"],
                "due_date": requirement["due_date"],
                "status": requirement["status"],
                "review_status": requirement["review_status"],
                "provenance": requirement["provenance"],
                "evidence": {
                    evidence["id"]: {
                        "status": evidence["status"],
                        "submitted_by_id": evidence["submitted_by_id"],
                    }
                    for evidence in requirement["evidence"]
                },
            }
            for requirement in snapshot["requirements"]
        },
    }
