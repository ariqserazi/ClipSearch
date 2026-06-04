import json
import logging
import re
from html import unescape
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urljoin, urlparse, urlunparse

import httpx
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Comment, Item, Media, Run, Source, utcnow
from app.ranking import rank_item
from app.schemas import RedditCollectRequest
from app.web_headers import web_client_kwargs

logger = logging.getLogger(__name__)


def _dt_from_epoch(value: float | int | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromtimestamp(float(value), tz=timezone.utc)


def _epoch_from_iso(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, default=str)


def _source(db: Session) -> Source:
    source = db.query(Source).filter(Source.name == "reddit").one_or_none()
    if not source:
        source = Source(name="reddit", type="reddit", base_url="https://www.reddit.com")
        db.add(source)
        db.flush()
    return source


def _mode_path(mode: str) -> tuple[str, dict[str, str]]:
    if mode == "top_day":
        return "top", {"t": "day"}
    if mode == "top_week":
        return "top", {"t": "week"}
    return mode, {}


def _attrs(tag: str) -> dict[str, str]:
    return {
        key: unescape(value)
        for key, _quote, value in re.findall(r'([\w:-]+)\s*=\s*([\'"])(.*?)\2', tag, flags=re.DOTALL)
    }


def _strip_html(value: str | None) -> str:
    if not value:
        return ""
    value = re.sub(r"(?is)<(br|p|div|li)\b[^>]*>", "\n", value)
    value = re.sub(r"(?is)<[^>]+>", "", value)
    value = unescape(value)
    return re.sub(r"\s+", " ", value).strip()


def _absolute_url(value: str | None, base: str = "https://www.reddit.com") -> str | None:
    if not value:
        return None
    if value.startswith("//"):
        return "https:" + value
    return urljoin(base, value)


def _subreddit_from_url(value: str | None) -> str | None:
    if not value:
        return None
    match = re.search(r"(?:^|/)r/([^/?#]+)", value, flags=re.IGNORECASE)
    return match.group(1) if match else None


def _reddit_web_url(value: str | None, subreddit: str, mode: str) -> str:
    if not value:
        path, _extra_params = _mode_path(mode)
        return f"https://old.reddit.com/r/{subreddit}/{path}/"
    raw = value.strip()
    if not raw.startswith(("http://", "https://")):
        raw = "https://" + raw.lstrip("/")
    parsed = urlparse(raw)
    netloc = parsed.netloc.lower()
    if netloc in {"reddit.com", "www.reddit.com", "new.reddit.com", "sh.reddit.com"}:
        netloc = "old.reddit.com"
    elif not netloc:
        netloc = "old.reddit.com"
    return urlunparse(parsed._replace(scheme="https", netloc=netloc))


def _int_attr(value: str | None) -> int | None:
    if value is None:
        return None
    match = re.search(r"-?\d+", value.replace(",", ""))
    return int(match.group(0)) if match else None


def _looks_like_video(url: str | None, domain: str | None) -> bool:
    text = f"{url or ''} {domain or ''}".lower()
    return any(
        marker in text
        for marker in (
            "v.redd.it",
            "clips.twitch.tv",
            "twitch.tv",
            "youtube.com",
            "youtu.be",
            "streamable.com",
            "kick.com",
            ".mp4",
            ".m3u8",
        )
    )


def _segments(html_text: str, fullname_prefix: str) -> list[tuple[str, str]]:
    pattern = re.compile(rf'<div\b[^>]*data-fullname="{re.escape(fullname_prefix)}[^"]+"[^>]*>', re.DOTALL)
    matches = list(pattern.finditer(html_text))
    result: list[tuple[str, str]] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(html_text)
        result.append((match.group(0), html_text[match.start() : end]))
    return result


def _parse_listing_posts(html_text: str, subreddit: str, limit: int) -> list[dict[str, Any]]:
    posts: list[dict[str, Any]] = []
    for tag, segment in _segments(html_text, "t3_"):
        attrs = _attrs(tag)
        fullname = attrs.get("data-fullname", "")
        external_id = fullname.removeprefix("t3_")
        if not external_id or attrs.get("data-promoted") == "true":
            continue

        title_match = re.search(r'<a\b[^>]*class="[^"]*\btitle\b[^"]*"[^>]*>(.*?)</a>', segment, re.DOTALL)
        time_match = re.search(r"<time\b[^>]*datetime=\"([^\"]+)\"", segment)
        comments_match = re.search(r'<a\b[^>]*class="[^"]*\bcomments\b[^"]*"[^>]*>(.*?)</a>', segment, re.DOTALL)
        thumbnail_match = re.search(r'<a\b[^>]*class="[^"]*\bthumbnail\b[^"]*"[^>]*>.*?<img\b[^>]*src="([^"]+)"', segment, re.DOTALL)
        flair_match = re.search(r'<span\b[^>]*class="[^"]*\blinkflairlabel\b[^"]*"[^>]*>(.*?)</span>', segment, re.DOTALL)

        url = _absolute_url(attrs.get("data-url"))
        permalink = _absolute_url(attrs.get("data-permalink"))
        comments_count = _int_attr(attrs.get("data-comments-count")) or _int_attr(_strip_html(comments_match.group(1)) if comments_match else None)
        score = _int_attr(attrs.get("data-score"))
        domain = attrs.get("data-domain")
        is_video = _looks_like_video(url, domain)
        is_reddit_video = bool(url and "v.redd.it" in url)

        post = {
            "id": external_id,
            "title": _strip_html(title_match.group(1) if title_match else ""),
            "author": attrs.get("data-author"),
            "created_utc": _epoch_from_iso(time_match.group(1) if time_match else None),
            "url_overridden_by_dest": url,
            "url": url,
            "permalink": attrs.get("data-permalink"),
            "domain": domain,
            "subreddit": attrs.get("data-subreddit") or subreddit,
            "link_flair_text": _strip_html(flair_match.group(1) if flair_match else ""),
            "thumbnail": _absolute_url(thumbnail_match.group(1), "https://old.reddit.com") if thumbnail_match else None,
            "selftext": None,
            "score": score,
            "upvote_ratio": None,
            "num_comments": comments_count,
            "is_video": is_video,
            "secure_media": {"reddit_video": {}} if not is_reddit_video else {"reddit_video": {"fallback_url": url}},
            "web_permalink": permalink,
            "web_collected": True,
        }
        posts.append(post)
        if len(posts) >= limit:
            break
    return posts


def _parse_comment_segments(html_text: str, limit: int) -> list[dict[str, Any]]:
    comments: list[dict[str, Any]] = []
    for tag, segment in _segments(html_text, "t1_"):
        attrs = _attrs(tag)
        external_id = attrs.get("data-fullname", "").removeprefix("t1_")
        if not external_id:
            continue
        body_match = re.search(r'<div\b[^>]*class="[^"]*\busertext-body\b[^"]*"[^>]*>(.*?)</div>\s*</div>', segment, re.DOTALL)
        time_match = re.search(r"<time\b[^>]*datetime=\"([^\"]+)\"", segment)
        score_match = re.search(r'<span\b[^>]*class="[^"]*\bscore\b[^"]*"[^>]*>(.*?)</span>', segment, re.DOTALL)
        body = _strip_html(body_match.group(1) if body_match else "")
        if not body:
            continue
        comments.append(
            {
                "id": external_id,
                "author": attrs.get("data-author"),
                "body": body,
                "score": _int_attr(attrs.get("data-score")) or _int_attr(_strip_html(score_match.group(1)) if score_match else None),
                "created_utc": _epoch_from_iso(time_match.group(1) if time_match else None),
                "web_collected": True,
            }
        )
        if len(comments) >= limit:
            break
    return comments


async def _token(client: httpx.AsyncClient) -> str:
    settings = get_settings()
    response = await client.post(
        "https://www.reddit.com/api/v1/access_token",
        auth=(settings.reddit_client_id, settings.reddit_client_secret),
        data={
            "grant_type": "password",
            "username": settings.reddit_username,
            "password": settings.reddit_password,
        },
        headers={"User-Agent": settings.reddit_user_agent},
    )
    response.raise_for_status()
    return response.json()["access_token"]


async def _top_comments(
    client: httpx.AsyncClient,
    token: str,
    subreddit: str,
    post_id: str,
    limit: int,
) -> list[dict[str, Any]]:
    if limit <= 0:
        return []
    settings = get_settings()
    response = await client.get(
        f"https://oauth.reddit.com/r/{subreddit}/comments/{post_id}",
        params={"limit": limit, "sort": "top", "depth": 1},
        headers={"Authorization": f"Bearer {token}", "User-Agent": settings.reddit_user_agent},
    )
    response.raise_for_status()
    payload = response.json()
    if len(payload) < 2:
        return []
    comments = []
    for child in payload[1].get("data", {}).get("children", [])[:limit]:
        if child.get("kind") != "t1":
            continue
        comments.append(child.get("data", {}))
    return comments


async def _top_comments_web(
    client: httpx.AsyncClient,
    permalink: str | None,
    limit: int,
) -> list[dict[str, Any]]:
    if limit <= 0 or not permalink:
        return []
    response = await client.get(
        _absolute_url(permalink, "https://old.reddit.com") or permalink,
        params={"sort": "top"},
    )
    response.raise_for_status()
    return _parse_comment_segments(response.text, limit)


def _upsert_post(db: Session, source: Source, post: dict[str, Any], comments: list[dict[str, Any]]) -> Item:
    external_id = post["id"]
    item = db.query(Item).filter(Item.source_id == source.id, Item.external_id == external_id).one_or_none()
    metrics = {
        "score": post.get("score"),
        "upvote_ratio": post.get("upvote_ratio"),
        "num_comments": post.get("num_comments"),
    }
    secure_media = post.get("secure_media") or {}
    reddit_video = secure_media.get("reddit_video") or {}
    is_reddit_video = bool(reddit_video)
    is_video = bool(post.get("is_video")) or is_reddit_video
    thumbnail = post.get("thumbnail")
    if thumbnail in {"self", "default", "nsfw", "spoiler", ""}:
        thumbnail = None

    values = {
        "source_id": source.id,
        "external_id": external_id,
        "title_or_text": post.get("title") or "",
        "author_name": post.get("author"),
        "created_time": _dt_from_epoch(post.get("created_utc")),
        "url": post.get("url_overridden_by_dest") or post.get("url") or f"https://www.reddit.com{post.get('permalink', '')}",
        "permalink": f"https://www.reddit.com{post.get('permalink', '')}" if post.get("permalink") else None,
        "domain": post.get("domain"),
        "subreddit": post.get("subreddit"),
        "flair": post.get("link_flair_text"),
        "thumbnail": thumbnail,
        "self_text": post.get("selftext"),
        "is_video": is_video,
        "is_reddit_hosted_video": is_reddit_video,
        "metrics_json": _json(metrics),
        "raw_json": _json(post),
        "deleted_or_removed": (post.get("removed_by_category") is not None) or (post.get("author") == "[deleted]"),
    }
    if not item:
        item = Item(**values)
        db.add(item)
        db.flush()
    else:
        for key, value in values.items():
            setattr(item, key, value)
        db.flush()

    db.query(Media).filter(Media.item_id == item.id).delete()
    if reddit_video:
        db.add(
            Media(
                item_id=item.id,
                media_type="reddit_video",
                url=reddit_video.get("fallback_url"),
                duration_ms=(reddit_video.get("duration") or 0) * 1000 if reddit_video.get("duration") else None,
                width=reddit_video.get("width"),
                height=reddit_video.get("height"),
                raw_json=_json(reddit_video),
            )
        )

    for comment in comments:
        external_comment_id = comment.get("id")
        if not external_comment_id:
            continue
        existing = db.query(Comment).filter(Comment.item_id == item.id, Comment.external_id == external_comment_id).one_or_none()
        values = {
            "item_id": item.id,
            "external_id": external_comment_id,
            "author_name": comment.get("author"),
            "body": comment.get("body") or "",
            "score": comment.get("score"),
            "created_time": _dt_from_epoch(comment.get("created_utc")),
            "raw_json": _json(comment),
        }
        if existing:
            for key, value in values.items():
                setattr(existing, key, value)
        else:
            db.add(Comment(**values))
    db.flush()
    rank_item(db, item)
    return item


async def collect_reddit(db: Session, request: RedditCollectRequest) -> dict[str, Any]:
    settings = get_settings()
    subreddit = request.subreddit or _subreddit_from_url(request.url) or settings.default_subreddit
    use_api = not request.url and (request.source_mode == "api" or (request.source_mode == "auto" and settings.reddit_configured))
    run_mode = request.mode if use_api else ("url_web" if request.url else f"{request.mode}_web")
    run = Run(
        source="reddit",
        mode=run_mode,
        status="running",
        raw_json=_json({"subreddit": subreddit, "url": request.url, "source_mode": request.source_mode}),
    )
    db.add(run)
    db.commit()

    if request.source_mode == "api" and not settings.reddit_configured:
        run.status = "failed"
        run.finished_at = utcnow()
        run.error = "Reddit API credentials are missing. Fill REDDIT_* values in .env."
        db.commit()
        return {"run_id": run.id, "status": run.status, "items_collected": 0, "error": run.error}

    try:
        if use_api:
            async with httpx.AsyncClient(timeout=30.0) as client:
                token = await _token(client)
                path, extra_params = _mode_path(request.mode)
                response = await client.get(
                    f"https://oauth.reddit.com/r/{subreddit}/{path}",
                    params={"limit": request.limit, **extra_params},
                    headers={"Authorization": f"Bearer {token}", "User-Agent": settings.reddit_user_agent},
                )
                logger.info(
                    "Reddit rate limit remaining=%s reset=%s used=%s",
                    response.headers.get("x-ratelimit-remaining"),
                    response.headers.get("x-ratelimit-reset"),
                    response.headers.get("x-ratelimit-used"),
                )
                response.raise_for_status()
                children = response.json().get("data", {}).get("children", [])
                source = _source(db)
                collected = 0
                for child in children:
                    post = child.get("data", {})
                    if not post.get("id"):
                        continue
                    comments = await _top_comments(client, token, subreddit, post["id"], request.top_comments_limit)
                    _upsert_post(db, source, post, comments)
                    collected += 1
                run.status = "success"
                run.items_collected = collected
                run.finished_at = utcnow()
                db.commit()
                return {"run_id": run.id, "status": run.status, "source_mode": "api", "items_collected": collected}
        async with httpx.AsyncClient(**web_client_kwargs(timeout=30.0, follow_redirects=True)) as client:
            path, extra_params = _mode_path(request.mode)
            web_url = _reddit_web_url(request.url, subreddit, request.mode)
            response = await client.get(
                web_url,
                params={"limit": request.limit, **extra_params},
            )
            response.raise_for_status()
            posts = _parse_listing_posts(response.text, subreddit, request.limit)
            source = _source(db)
            collected = 0
            for post in posts:
                comments = await _top_comments_web(client, post.get("web_permalink") or post.get("permalink"), request.top_comments_limit)
                _upsert_post(db, source, post, comments)
                collected += 1
            run.status = "success"
            run.items_collected = collected
            run.finished_at = utcnow()
            db.commit()
            return {"run_id": run.id, "status": run.status, "source_mode": "web", "items_collected": collected, "url": web_url}
    except Exception as exc:
        db.rollback()
        run = db.get(Run, run.id)
        if run:
            run.status = "failed"
            run.finished_at = utcnow()
            run.error = str(exc)
            db.commit()
            return {"run_id": run.id, "status": run.status, "items_collected": run.items_collected, "error": run.error}
        raise
