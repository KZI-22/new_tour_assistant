from __future__ import annotations

from datetime import datetime
from typing import Literal, Self
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class XhsPlanningModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class XhsPostImage(XhsPlanningModel):
    index: int = Field(ge=1)
    width: int = Field(ge=1)
    height: int = Field(ge=1)
    preview_url: str = Field(min_length=1, max_length=4096)
    original_url: str = Field(min_length=1, max_length=4096)
    live_photo: bool = False

    @field_validator("preview_url", "original_url")
    @classmethod
    def validate_cdn_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        hostname = (parsed.hostname or "").casefold()
        if (
            parsed.scheme.casefold() != "https"
            or not hostname.endswith(".xhscdn.com")
            or parsed.username is not None
            or parsed.password is not None
            or any(character.isspace() for character in value)
            or "<" in value
            or ">" in value
        ):
            raise ValueError("image URL must use HTTPS on an xhscdn.com host")
        return value


class XhsPostEvidence(XhsPlanningModel):
    reference_id: Literal["source_1", "source_2"]
    role: Literal["primary", "supplementary"]
    note_id: str
    search_rank: int = Field(ge=1)
    title: str
    author_name: str
    published_at: str | None = None
    content: str = Field(min_length=1)
    liked_count_raw: str | None = None
    liked_count: int | None = Field(default=None, ge=0)
    queried_at: datetime
    images: list[XhsPostImage] = Field(default_factory=list)


class XhsResearchResult(XhsPlanningModel):
    keyword: str
    posts: list[XhsPostEvidence] = Field(min_length=1, max_length=2)
    warnings: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_source_roles(self) -> Self:
        expected = [("source_1", "primary")]
        if len(self.posts) == 2:
            expected.append(("source_2", "supplementary"))
        actual = [(post.reference_id, post.role) for post in self.posts]
        if actual != expected:
            raise ValueError(
                "posts must contain primary source_1 then optional supplementary source_2"
            )
        return self


__all__ = [
    "XhsPostImage",
    "XhsPostEvidence",
    "XhsResearchResult",
]
