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
