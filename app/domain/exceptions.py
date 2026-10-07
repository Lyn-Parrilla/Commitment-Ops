"""Domain errors for validation and illegal lifecycle changes."""


class DomainError(Exception):
    """Base error for commitment-domain rule violations."""


class DomainValidationError(DomainError):
    """Raised when required information is missing or malformed."""


class InvalidTransitionError(DomainError):
    """Raised when a status or review transition is not allowed."""
