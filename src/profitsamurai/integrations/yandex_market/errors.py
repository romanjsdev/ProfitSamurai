"""Partner API failures."""


class YandexMarketError(Exception):
    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class RateLimitError(YandexMarketError):
    """HTTP 420 or 429. Safe to retry."""


class TransientStatusError(YandexMarketError):
    """HTTP 5xx. Safe to retry."""
