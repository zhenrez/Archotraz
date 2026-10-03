class ArchotrazError(Exception):
    """Base exception for ARCHOTRAZ."""


class IdempotencyConflict(ArchotrazError):
    """Raised when an idempotency key is reused for a different request."""


class NotFound(ArchotrazError):
    """Raised when an authoritative record does not exist."""


class IntegrityFailure(ArchotrazError):
    """Raised when referenced evidence bytes fail integrity verification."""


class VersionConflict(ArchotrazError):
    """Raised when a state transition targets a stale candidate version."""
