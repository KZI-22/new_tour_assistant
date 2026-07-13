from __future__ import annotations

from app.schemas.amap import AmapErrorCode


class AmapError(RuntimeError):
    """Base error with a safe, provider-independent message."""

    error_code = AmapErrorCode.PROVIDER_ERROR

    def __init__(self, message: str, *, infocode: str | None = None) -> None:
        super().__init__(message)
        self.infocode = infocode


class AmapConfigurationError(AmapError):
    error_code = AmapErrorCode.CONFIGURATION_ERROR


class AmapRequestError(AmapError):
    error_code = AmapErrorCode.REQUEST_ERROR


class AmapTimeoutError(AmapError):
    error_code = AmapErrorCode.TIMEOUT


class AmapRateLimitError(AmapError):
    error_code = AmapErrorCode.RATE_LIMITED


class AmapInvalidParameterError(AmapError):
    error_code = AmapErrorCode.INVALID_PARAMETER


class AmapEmptyResultError(AmapError):
    error_code = AmapErrorCode.EMPTY_RESULT


__all__ = [
    "AmapConfigurationError",
    "AmapEmptyResultError",
    "AmapError",
    "AmapInvalidParameterError",
    "AmapRateLimitError",
    "AmapRequestError",
    "AmapTimeoutError",
]
