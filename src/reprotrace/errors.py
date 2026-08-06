"""Domain errors with stable command-line exit semantics."""


class ReproTraceError(Exception):
    """Base class for expected ReproTrace failures."""


class ConfigError(ReproTraceError):
    """Raised when a manifest or evidence bundle is invalid."""
