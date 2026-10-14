"""Predictable persistence-boundary failures."""


class PersistenceError(Exception):
    """Base error for database persistence failures."""


class DatabaseConfigurationError(PersistenceError):
    """Raised when durable storage configuration is unavailable."""


class InvalidAssociationError(PersistenceError):
    """Raised when an entity is duplicated or attached inconsistently."""
