"""Member use cases for requirement-specific submission links."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from app.application.exceptions import (
    EvidenceFileUnavailableError,
    RequirementNotFoundError,
    ResourceNotFoundError,
    SubmissionLinkNotFoundError,
    WorkflowNotFoundError,
)
from app.domain.enums import RequirementStatus, WorkflowStatus
from app.domain.exceptions import InvalidTransitionError
from app.domain.submissions import (
    SUBMISSION_LINK_MAX_USES,
    SUBMISSION_LINK_TTL_DAYS,
    generate_submission_token,
    submission_token_hash,
)
from app.persistence.repository import WorkflowRepository
from app.persistence.submissions import StoredSubmissionLink, SubmissionRepository


@dataclass(frozen=True)
class IssuedSubmissionLink:
    link: StoredSubmissionLink
    submission_path: str


class SubmissionService:
    """Create and revoke links for a workflow the caller can already read."""

    def __init__(
        self,
        workflows: WorkflowRepository,
        submissions: SubmissionRepository,
        *,
        organization_id: UUID,
        user_id: UUID,
    ) -> None:
        self._workflows = workflows
        self._submissions = submissions
        self._organization_id = organization_id
        self._user_id = user_id

    def create_link(
        self,
        workflow_id: UUID,
        requirement_id: UUID,
    ) -> IssuedSubmissionLink:
        workflow = self._require_workflow(workflow_id)
        requirement = _require_requirement(workflow.requirements, requirement_id)
        if (
            workflow.status not in {WorkflowStatus.DRAFT, WorkflowStatus.ACTIVE}
            or requirement.status == RequirementStatus.CANCELLED
        ):
            raise InvalidTransitionError(
                "cannot create a submission link for a closed workflow "
                "or a cancelled requirement"
            )
        raw_token = generate_submission_token()
        stored = self._submissions.create_link(
            organization_id=self._organization_id,
            workflow_id=workflow.id,
            requirement_id=requirement.id,
            token_hash=submission_token_hash(raw_token),
            created_by_user_id=self._user_id,
            expires_at=datetime.now(UTC) + timedelta(days=SUBMISSION_LINK_TTL_DAYS),
            max_uses=SUBMISSION_LINK_MAX_USES,
            user_id=self._user_id,
        )
        return IssuedSubmissionLink(
            link=stored,
            submission_path=f"#/s/{raw_token}",
        )

    def evidence_file_status(
        self,
        workflow_id: UUID,
        requirement_id: UUID,
        evidence_id: UUID,
    ) -> None:
        """Authorize a member, then refuse the file until it has been scanned."""

        workflow = self._require_workflow(workflow_id)
        _require_requirement(workflow.requirements, requirement_id)
        status = self._submissions.file_download_status(
            organization_id=self._organization_id,
            user_id=self._user_id,
            workflow_id=workflow_id,
            requirement_id=requirement_id,
            evidence_id=evidence_id,
        )
        if status == "missing":
            raise ResourceNotFoundError("evidence was not found")
        raise EvidenceFileUnavailableError

    def list_links(
        self,
        workflow_id: UUID,
        requirement_id: UUID,
    ) -> list[StoredSubmissionLink]:
        workflow = self._require_workflow(workflow_id)
        _require_requirement(workflow.requirements, requirement_id)
        return self._submissions.list_links(
            organization_id=self._organization_id,
            workflow_id=workflow.id,
            requirement_id=requirement_id,
            user_id=self._user_id,
        )

    def revoke_link(
        self,
        workflow_id: UUID,
        requirement_id: UUID,
        link_id: UUID,
    ) -> StoredSubmissionLink:
        workflow = self._require_workflow(workflow_id)
        _require_requirement(workflow.requirements, requirement_id)
        stored = self._submissions.revoke_link(
            organization_id=self._organization_id,
            workflow_id=workflow.id,
            requirement_id=requirement_id,
            link_id=link_id,
            user_id=self._user_id,
        )
        if stored is None:
            raise SubmissionLinkNotFoundError(link_id)
        return stored

    def _require_workflow(self, workflow_id: UUID):
        workflow = self._workflows.get(
            workflow_id,
            organization_id=self._organization_id,
            user_id=self._user_id,
        )
        if workflow is None:
            raise WorkflowNotFoundError(workflow_id)
        return workflow


def _require_requirement(requirements, requirement_id: UUID):
    for requirement in requirements:
        if requirement.id == requirement_id:
            return requirement
    raise RequirementNotFoundError(requirement_id)
