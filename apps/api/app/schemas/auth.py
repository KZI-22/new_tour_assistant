from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class AuthModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class SendSmsCodeRequest(AuthModel):
    phone: str = Field(min_length=11, max_length=32)


class SendSmsCodeResponse(AuthModel):
    challenge_id: UUID
    expires_in: int = Field(gt=0)
    resend_after: int = Field(gt=0)
    debug_code: str | None = Field(default=None, pattern=r"^\d{6}$")


class PhoneLoginRequest(AuthModel):
    challenge_id: UUID
    phone: str = Field(min_length=11, max_length=32)
    code: str = Field(pattern=r"^\d{6}$")


class AuthUserResponse(AuthModel):
    id: UUID
    phone: str
    display_name: str | None


class LoginResponse(AuthModel):
    user: AuthUserResponse
    is_new_user: bool
    access_expires_in: int = Field(gt=0)


__all__ = [
    "AuthUserResponse",
    "LoginResponse",
    "PhoneLoginRequest",
    "SendSmsCodeRequest",
    "SendSmsCodeResponse",
]
