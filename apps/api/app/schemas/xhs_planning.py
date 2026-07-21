from __future__ import annotations

from datetime import datetime
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class XhsPlanningModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


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
    "XhsPostEvidence",
    "XhsResearchResult",
]
