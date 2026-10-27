"""Application-boundary failures independent of HTTP and persistence details."""

from uuid import UUID


class ResourceNotFoundError(Exception):
    """Raised when a requested aggregate or child entity does not exist."""


class WorkflowNotFoundError(ResourceNotFoundError):
    def __init__(self, workflow_id: UUID) -> None:
        super().__init__(f"workflow {workflow_id} was not found")


class RequirementNotFoundError(ResourceNotFoundError):
    def __init__(self, requirement_id: UUID) -> None:
        super().__init__(f"requirement {requirement_id} was not found")


class CandidateNotFoundError(ResourceNotFoundError):
    def __init__(self, candidate_id: UUID) -> None:
        super().__init__(f"candidate {candidate_id} was not found")


class LearningSignalNotFoundError(ResourceNotFoundError):
    def __init__(self, signal_id: UUID) -> None:
        super().__init__(f"learning signal {signal_id} was not found")


class AssistanceProposalNotFoundError(ResourceNotFoundError):
    def __init__(self, proposal_id: UUID) -> None:
        super().__init__(f"assistance proposal {proposal_id} was not found")


class SubmissionLinkNotFoundError(ResourceNotFoundError):
    def __init__(self, link_id: UUID) -> None:
        super().__init__(f"submission link {link_id} was not found")


class SubmissionLinkUnavailableError(ResourceNotFoundError):
    """Public failure that does not reveal why a submission link cannot be used."""

    def __init__(self) -> None:
        super().__init__("This submission link is not valid.")


class EvidenceFileUnavailableError(Exception):
    """Raised when a stored file must not be downloaded."""

    def __init__(self) -> None:
        super().__init__("This file is not available for download.")
