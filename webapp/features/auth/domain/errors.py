"""Authentication persistence errors independent of storage adapters."""


class AuthStoreError(RuntimeError):
    """Base error for durable authentication persistence failures."""


class DuplicateCredentialError(AuthStoreError):
    """Raised when a globally unique credential ID already exists."""


class CredentialLimitError(AuthStoreError):
    """Raised when an account already owns the maximum credential count."""


class ChallengeLimitError(AuthStoreError):
    """Raised when an account has too many registration challenges."""
