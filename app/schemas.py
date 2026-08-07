from typing import Any, Literal

from pydantic import BaseModel, Field


class RedditCollectRequest(BaseModel):
    subreddit: str | None = None
    url: str | None = None
    query: str | None = None
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


class KiwiFarmsCollectRequest(BaseModel):
    query: str = Field(min_length=1, max_length=200)
    limit: int = Field(default=25, ge=1, le=100)
    max_pages: int | None = Field(default=None, ge=1, le=25)


class XFromRedditRequest(BaseModel):
    query: str | None = None
    accounts: list[str] | None = None
    time_window: Literal["day", "week", "month", "year", "all"] = "month"
    limit: int = Field(default=50, ge=1, le=100)


class XArchiveSearchRequest(BaseModel):
    account: str | None = None
    person: str | None = None
    topic: str | None = None
    search_provider: Literal["web", "google", "archive", "all"] = "web"
    archive_urls: list[str] = Field(default_factory=list)
    archive_html: str | None = None
    limit: int = Field(default=25, ge=1, le=100)


class XArchiveImportRequest(BaseModel):
    path: str
    account: str | None = None
    limit: int = Field(default=100, ge=1, le=5000)


class CollectAllRequest(BaseModel):
    reddit: RedditCollectRequest = Field(default_factory=RedditCollectRequest)
    x: XCollectRequest = Field(default_factory=XCollectRequest)
    kiwifarms: KiwiFarmsCollectRequest | None = None


class ClipFilters(BaseModel):
    source: Literal["reddit", "x", "reddit_x", "kiwifarms", "all"] = "all"
    min_drama_score: float = Field(default=0, ge=0, le=100)
    time_window: Literal["day", "week", "month", "year", "all"] = "week"
    has_video: bool | None = None
    keyword: str | None = None
    account: str | None = None
    limit: int = Field(default=50, ge=1, le=200)


class AgentSearchRequest(BaseModel):
    source: Literal["reddit", "x", "reddit_x", "kiwifarms", "all"] = "all"
    time_window: Literal["day", "week", "month", "year", "all"] = "day"
    keywords: list[str] = Field(default_factory=list)
    account: str | None = None
    min_drama_score: float = Field(default=70, ge=0, le=100)
    has_video: bool | None = None
    limit: int = Field(default=20, ge=1, le=100)


class ClipResult(BaseModel):
    id: int
    title_or_text: str
    source: str
    url: str
    permalink: str | None = None
    thumbnail: str | None = None
    author_name: str | None = None
    is_video: bool = False
    drama_score: float
    potential_label: str
    reasoning: str
    created_time: str | None = None
    metrics: dict[str, Any] = Field(default_factory=dict)
    media: dict[str, Any] = Field(default_factory=dict)
    why_it_may_be_useful: str


class AgentSearchResponse(BaseModel):
    results: list[ClipResult]


class PinterestImageResearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=300)
    limit: int = Field(default=8, ge=1, le=50)


class LinksDownloadRequest(BaseModel):
    urls: list[str] = Field(min_length=1, max_length=100)


class XLinksDownloadRequest(LinksDownloadRequest):
    pass
