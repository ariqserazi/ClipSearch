from typing import Any, Literal

from pydantic import BaseModel, Field


class RedditCollectRequest(BaseModel):
    subreddit: str | None = None
    url: str | None = None
    mode: Literal["hot", "new", "rising", "top_day", "top_week"] = "hot"
    source_mode: Literal["auto", "api", "web"] = "auto"
    limit: int = Field(default=50, ge=1, le=100)
    top_comments_limit: int = Field(default=5, ge=0, le=10)


class XCollectRequest(BaseModel):
    query: str | None = None
    accounts: list[str] | None = None
    urls: list[str] | None = None
    source_mode: Literal["auto", "api", "web"] = "auto"
    limit: int = Field(default=50, ge=10, le=100)


class CollectAllRequest(BaseModel):
    reddit: RedditCollectRequest = Field(default_factory=RedditCollectRequest)
    x: XCollectRequest = Field(default_factory=XCollectRequest)


class ClipFilters(BaseModel):
    source: Literal["reddit", "x", "all"] = "all"
    min_drama_score: float = Field(default=0, ge=0, le=100)
    time_window: Literal["day", "week", "all"] = "week"
    has_video: bool | None = None
    keyword: str | None = None
    limit: int = Field(default=50, ge=1, le=200)


class AgentSearchRequest(BaseModel):
    source: Literal["reddit", "x", "all"] = "all"
    time_window: Literal["day", "week"] = "day"
    keywords: list[str] = Field(default_factory=list)
    min_drama_score: float = Field(default=70, ge=0, le=100)
    limit: int = Field(default=20, ge=1, le=100)


class ClipResult(BaseModel):
    id: int
    title_or_text: str
    source: str
    url: str
    thumbnail: str | None = None
    drama_score: float
    potential_label: str
    reasoning: str
    created_time: str | None = None
    metrics: dict[str, Any] = Field(default_factory=dict)
    media: dict[str, Any] = Field(default_factory=dict)
    why_it_may_be_useful: str


class AgentSearchResponse(BaseModel):
    results: list[ClipResult]
