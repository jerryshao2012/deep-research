"""Safe failure type for structural citation enforcement."""


class ReportCitationError(RuntimeError):
    """Raised when report citation requirements are not satisfied."""
