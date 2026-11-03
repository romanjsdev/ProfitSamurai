"""Partner API failures."""


class YandexMarketError(Exception):
    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class RateLimitError(YandexMarketError):
    """HTTP 420 or 429. The call was rejected and may be repeated after the delay."""

    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        wait_seconds: float = 120,
    ) -> None:
        super().__init__(message, status_code)
        self.wait_seconds = wait_seconds


class TransientStatusError(YandexMarketError):
    """HTTP 5xx. Safe to retry."""
