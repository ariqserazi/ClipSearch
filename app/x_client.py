import base64
import json
import re
from datetime import datetime
from email.utils import parsedate_to_datetime
from html import unescape
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote_plus, unquote, urlparse

import httpx
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Comment, Item, Media, Run, Source, utcnow
from app.ranking import KEYWORDS, rank_item
from app.reports import since_for_window
from app.schemas import XArchiveImportRequest, XArchiveSearchRequest, XCollectRequest, XFromRedditRequest
from app.web_headers import web_client_kwargs

X_STATUS_RE = re.compile(r"https?://(?:(?:www|mobile)\.)?(?:x|twitter)\.com/([A-Za-z0-9_]{1,20})/status(?:es)?/(\d+)", re.IGNORECASE)
ARCHIVE_DOMAINS = ("archive.ph", "archive.vn", "archive.is", "archive.today", "archive.md", "archive.fo", "archive.li")
ARCHIVE_URL_RE = re.compile(r"https?://(?:archive\.(?:ph|vn|is|today|md|fo|li))/[^\s\"'<>]+", re.IGNORECASE)
HREF_RE = re.compile(r"""href=["']([^"']+)["']""", re.IGNORECASE)
WEB_SEARCH_PAGE_LIMIT = 10


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, default=str)


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:
            return parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None


def _parse_x_state(html: str) -> dict[str, Any]:
    marker = "window.__INITIAL_STATE__="
    start = html.find(marker)
    if start == -1:
        return {}
    payload = html[start + len(marker) :]
    decoder = json.JSONDecoder()
    for candidate in (payload, unescape(payload)):
        try:
            state, _end = decoder.raw_decode(candidate)
            return state if isinstance(state, dict) else {}
        except json.JSONDecodeError:
            continue
    return {}


def _state_entities(state: dict[str, Any], name: str) -> dict[str, dict[str, Any]]:
    entities = state.get("entities", {}).get(name, {}).get("entities", {})
    return entities if isinstance(entities, dict) else {}


def _media_from_tweet(tweet: dict[str, Any]) -> dict[str, dict[str, Any]]:
    media_entries = (tweet.get("extended_entities") or tweet.get("entities") or {}).get("media") or []
    media: dict[str, dict[str, Any]] = {}
    for index, entry in enumerate(media_entries):
        key = entry.get("media_key") or entry.get("id_str") or f"{tweet.get('id_str') or tweet.get('id')}-{index}"
        media[key] = {
            "media_key": key,
            "type": entry.get("type"),
            "url": entry.get("media_url_https") or entry.get("media_url"),
            "preview_image_url": entry.get("media_url_https") or entry.get("media_url"),
            "duration_ms": (entry.get("video_info") or {}).get("duration_millis"),
            "width": ((entry.get("original_info") or {}).get("width") or (entry.get("sizes", {}).get("large") or {}).get("w")),
            "height": ((entry.get("original_info") or {}).get("height") or (entry.get("sizes", {}).get("large") or {}).get("h")),
            "variants": (entry.get("video_info") or {}).get("variants") or [],
        }
    return media


def _html_content(html: str, pattern: str) -> str | None:
    match = re.search(pattern, html, re.IGNORECASE | re.DOTALL)
    return unescape(match.group(1)).strip() if match else None


def _meta_content(html: str, key: str) -> str | None:
    escaped = re.escape(key)
    patterns = [
        rf'<meta\b[^>]*(?:property|name)=["\']{escaped}["\'][^>]*content=["\']([^"\']*)["\']',
        rf'<meta\b[^>]*content=["\']([^"\']*)["\'][^>]*(?:property|name)=["\']{escaped}["\']',
    ]
    for pattern in patterns:
        value = _html_content(html, pattern)
        if value:
            return value
    return None


def _best_status_thumbnail(html: str) -> str | None:
    candidates = re.findall(r'<link\b[^>]*rel=["\']preload["\'][^>]*as=["\']image["\'][^>]*href=["\']([^"\']*)["\']', html, re.IGNORECASE)
    candidates.extend(re.findall(r'<link\b[^>]*href=["\']([^"\']*)["\'][^>]*rel=["\']preload["\'][^>]*as=["\']image["\']', html, re.IGNORECASE))
    candidates.extend([_meta_content(html, "og:image") or "", _meta_content(html, "twitter:image") or ""])
    clean = [unescape(candidate).strip() for candidate in candidates if candidate]
    preferred_markers = ("amplify_video_thumb", "ext_tw_video_thumb", "tweet_video_thumb", "/media/")
    for candidate in clean:
        if any(marker in candidate for marker in preferred_markers):
            return candidate
    for candidate in clean:
        if "profile_images" not in candidate:
            return candidate
    return clean[0] if clean else None


def _tweet_from_public_status_html(html: str, url: str) -> tuple[dict[str, Any], dict[str, dict[str, Any]], dict[str, dict[str, Any]]] | None:
    match = X_STATUS_RE.search(url)
    if not match:
        return None
    handle, status_id = match.group(1), match.group(2)
    text = _meta_content(html, "og:description") or ""
    if not text:
        title = _html_content(html, r"<title>(.*?)</title>") or ""
        text_match = re.search(r'on X:\s*["“](.*?)["”]\s*/\s*X', title, re.IGNORECASE | re.DOTALL)
        text = text_match.group(1).strip() if text_match else title.replace(" / X", "").strip()
    if not text:
        return None

    title = _html_content(html, r"<title>(.*?)</title>") or ""
    author_name = handle
    author_match = re.search(r"^(.*?)\s+on X:", title, re.IGNORECASE | re.DOTALL)
    if author_match:
        author_name = author_match.group(1).strip() or handle
    author_title = _meta_content(html, "og:title") or ""
    handle_match = re.search(r"\(@([A-Za-z0-9_]{1,20})\)", author_title)
    if handle_match:
        handle = handle_match.group(1)

    thumbnail = _best_status_thumbnail(html)
    media_key = f"{status_id}-public-preview"
    media: dict[str, dict[str, Any]] = {}
    if thumbnail:
        media[media_key] = {
            "media_key": media_key,
            "type": "video",
            "url": None,
            "preview_image_url": thumbnail,
            "duration_ms": None,
            "width": None,
            "height": None,
            "variants": [],
            "public_status_html_collected": True,
        }
    tweet = {
        "id": status_id,
        "text": text,
        "author_id": handle,
        "created_at": _meta_content(html, "article:published_time"),
        "public_metrics": {},
        "attachments": {"media_keys": list(media.keys())},
        "public_status_html_collected": True,
    }
    users = {
        handle: {
            "id": handle,
            "username": handle,
            "name": author_name,
            "raw_public_status_user": {"og_title": author_title},
        }
    }
    return tweet, users, media


def _tweet_from_web(tweet: dict[str, Any]) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    media = _media_from_tweet(tweet)
    api_tweet = {
        "id": tweet.get("id_str") or tweet.get("id"),
        "text": tweet.get("full_text") or tweet.get("text") or "",
        "author_id": tweet.get("user") or tweet.get("user_id_str") or tweet.get("user_id"),
        "created_at": tweet.get("created_at"),
        "public_metrics": {
            "like_count": tweet.get("favorite_count"),
            "retweet_count": tweet.get("retweet_count"),
            "reply_count": tweet.get("reply_count"),
            "quote_count": tweet.get("quote_count"),
            "bookmark_count": tweet.get("bookmark_count"),
        },
        "attachments": {"media_keys": list(media.keys())},
        "web_collected": True,
        "raw_web_tweet": tweet,
    }
    return api_tweet, media


def _int_or_none(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _screen_name_from_tweet(tweet: dict[str, Any], fallback: str | None = None) -> str | None:
    user = tweet.get("user")
    if isinstance(user, dict):
        return user.get("screen_name") or user.get("username") or fallback
    return fallback


def _tweet_from_archive(
    tweet: dict[str, Any],
    fallback_account: str | None,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    media = _media_from_tweet(tweet)
    author_name = _screen_name_from_tweet(tweet, fallback_account)
    author_id = tweet.get("user_id_str") or tweet.get("user_id") or author_name or "archive"
    api_tweet = {
        "id": tweet.get("id_str") or tweet.get("id"),
        "text": tweet.get("full_text") or tweet.get("text") or "",
        "author_id": str(author_id),
        "created_at": tweet.get("created_at"),
        "public_metrics": {
            "like_count": _int_or_none(tweet.get("favorite_count")),
            "retweet_count": _int_or_none(tweet.get("retweet_count")),
            "reply_count": _int_or_none(tweet.get("reply_count")),
            "quote_count": _int_or_none(tweet.get("quote_count")),
            "bookmark_count": _int_or_none(tweet.get("bookmark_count")),
        },
        "attachments": {"media_keys": list(media.keys())},
        "archive_collected": True,
        "raw_archive_tweet": tweet,
    }
    users = {
        str(author_id): {
            "id": str(author_id),
            "username": author_name,
            "name": author_name,
            "raw_archive_user": tweet.get("user") if isinstance(tweet.get("user"), dict) else {},
        }
    }
    return api_tweet, users, media


def _users_from_web(users: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        user_id: {
            "id": user_id,
            "username": user.get("screen_name") or user.get("username"),
            "name": user.get("name"),
            "verified": user.get("verified") or user.get("is_blue_verified"),
            "public_metrics": {
                "followers_count": user.get("followers_count") or user.get("normal_followers_count"),
                "following_count": user.get("friends_count"),
                "tweet_count": user.get("statuses_count"),
                "listed_count": user.get("listed_count"),
            },
            "raw_web_user": user,
        }
        for user_id, user in users.items()
    }


def _normalize_x_url(value: str) -> str:
    value = value.strip()
    if not value:
        return value
    if not value.startswith(("http://", "https://")):
        value = f"https://x.com/{value.lstrip('@')}"
    return value.replace("https://twitter.com/", "https://x.com/").replace("http://twitter.com/", "https://x.com/")


def _x_search_url(query: str) -> str:
    return f"https://x.com/search?q={quote_plus(query)}&src=typed_query&f=live"


def _x_status_urls_from_text(text: str | None) -> list[str]:
    if not text:
        return []
    return [f"https://x.com/{match.group(1)}/status/{match.group(2)}" for match in X_STATUS_RE.finditer(text)]


def _friendly_x_error(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        status_code = exc.response.status_code
        if status_code == 402:
            return (
                "X API returned 402 Payment Required. The bearer token is present, but this app needs X API "
                "pay-per-use credits or billing access for read/search endpoints."
            )
        if status_code == 401:
            return "X API returned 401 Unauthorized. Check that X_BEARER_TOKEN is copied correctly."
        if status_code == 403:
            return "X API returned 403 Forbidden. This app/token may not have access to the requested X API endpoint."
        if status_code == 429:
            return "X API returned 429 Too Many Requests. Wait for the rate limit window to reset and try a smaller limit."
    return str(exc)


def _friendly_fetch_error(exc: Exception, service: str) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        return f"{service} returned HTTP {exc.response.status_code}."
    return str(exc)


def _archive_blocked_note() -> str:
    return (
        "Archive.ph blocked automated index access with HTTP 429/security check. "
        "Open archive.ph in your browser and paste archive result pages or direct X status URLs."
    )


def _archive_response_is_blocked(response: httpx.Response) -> bool:
    text = response.text.lower()
    return response.status_code == 429 or "please complete the security check" in text or "too many requests" in text


def _source(db: Session) -> Source:
    source = db.query(Source).filter(Source.name == "x").one_or_none()
    if not source:
        source = Source(name="x", type="x", base_url="https://x.com")
        db.add(source)
        db.flush()
    return source


def _query(request: XCollectRequest) -> str:
    settings = get_settings()
    parts: list[str] = []
    if request.query:
        parts.append(f"({request.query})")
    else:
        parts.append("(" + " OR ".join(f'"{kw}"' if " " in kw else kw for kw in KEYWORDS) + ")")

    accounts = request.accounts if request.accounts is not None else settings.x_accounts
    clean_accounts = [account.strip().lstrip("@") for account in accounts if account.strip()]
    if clean_accounts:
        parts.append("(" + " OR ".join(f"from:{account}" for account in clean_accounts) + ")")
    parts.append("-is:retweet")
    return " ".join(parts)


def _upsert_post(
    db: Session,
    source: Source,
    tweet: dict[str, Any],
    users: dict[str, dict[str, Any]],
    media: dict[str, dict[str, Any]],
) -> Item:
    external_id = tweet["id"]
    author = users.get(tweet.get("author_id") or "", {})
    metrics = tweet.get("public_metrics") or {}
    source_url = f"https://x.com/{author.get('username', 'i')}/status/{external_id}"
    item_media = [media[key] for key in (tweet.get("attachments", {}).get("media_keys") or []) if key in media]
    thumbnail = next((entry.get("preview_image_url") or entry.get("url") for entry in item_media if entry.get("preview_image_url") or entry.get("url")), None)
    is_video = any(entry.get("type") in {"video", "animated_gif"} for entry in item_media)
    item = db.query(Item).filter(Item.source_id == source.id, Item.external_id == external_id).one_or_none()
    values = {
        "source_id": source.id,
        "external_id": external_id,
        "title_or_text": tweet.get("text") or "",
        "author_name": author.get("username") or tweet.get("author_id"),
        "created_time": _parse_dt(tweet.get("created_at")),
        "url": source_url,
        "permalink": source_url,
        "thumbnail": thumbnail,
        "is_video": is_video,
        "metrics_json": _json(metrics),
        "raw_json": _json({"tweet": tweet, "author": author}),
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
    for entry in item_media:
        db.add(
            Media(
                item_id=item.id,
                media_key=entry.get("media_key"),
                media_type=entry.get("type"),
                url=entry.get("url"),
                preview_image_url=entry.get("preview_image_url"),
                duration_ms=entry.get("duration_ms"),
                width=entry.get("width"),
                height=entry.get("height"),
                variants_json=_json(entry.get("variants") or []),
                raw_json=_json(entry),
            )
        )
    db.flush()
    rank_item(db, item)
    return item


async def _collect_x_web_page(
    db: Session,
    client: httpx.AsyncClient,
    source: Source,
    url: str,
    remaining: int,
) -> tuple[int, list[str], list[str]]:
    response = await client.get(url)
    response.raise_for_status()
    state = _parse_x_state(response.text)
    raw_users = _state_entities(state, "users")
    raw_tweets = _state_entities(state, "tweets")
    users = _users_from_web(raw_users)
    collected = 0
    media: dict[str, dict[str, Any]] = {}
    for raw_tweet in raw_tweets.values():
        tweet, tweet_media = _tweet_from_web(raw_tweet)
        if not tweet.get("id") or not tweet.get("text"):
            continue
        media.update(tweet_media)
        _upsert_post(db, source, tweet, users, media)
        collected += 1
        if collected >= remaining:
            break

    if collected == 0 and X_STATUS_RE.search(str(response.url)):
        public_status = _tweet_from_public_status_html(response.text, str(response.url))
        if public_status:
            tweet, public_users, public_media = public_status
            _upsert_post(db, source, tweet, public_users, public_media)
            collected = 1

    pinned_urls: list[str] = []
    for user in raw_users.values():
        screen_name = user.get("screen_name") or user.get("username") or "i"
        for tweet_id in user.get("pinned_tweet_ids_str") or []:
            pinned_urls.append(f"https://x.com/{screen_name}/status/{tweet_id}")

    notes: list[str] = []
    if raw_users and not raw_tweets and "/status/" not in str(response.url):
        handles = ", ".join(
            f"@{user.get('screen_name') or user.get('username')}"
            for user in raw_users.values()
            if user.get("screen_name") or user.get("username")
        )
        profile = f" for {handles}" if handles else ""
        notes.append(
            f"X returned profile metadata{profile}, but did not expose timeline tweets in logged-out page HTML. "
            "Paste direct status URLs or configure X_BEARER_TOKEN for timeline/search collection."
        )
    return collected, pinned_urls, notes


async def _collect_x_web(db: Session, request: XCollectRequest, run: Run) -> dict[str, Any]:
    settings = get_settings()
    seed_urls = [_normalize_x_url(url) for url in (request.urls or []) if url.strip()]
    accounts = request.accounts if request.accounts is not None else settings.x_accounts
    seed_urls.extend(f"https://x.com/{account.strip().lstrip('@')}" for account in accounts if account.strip())
    if request.query:
        seed_urls.append(_x_search_url(request.query))
    if not seed_urls:
        seed_urls.append(_x_search_url(_query(request)))

    source = _source(db)
    collected = 0
    pages_seen = 0
    notes: list[str] = []
    seen = set()
    queue = list(dict.fromkeys(seed_urls))
    try:
        async with httpx.AsyncClient(**web_client_kwargs(timeout=30.0, follow_redirects=True)) as client:
            while queue and collected < request.limit:
                url = queue.pop(0)
                if not url or url in seen:
                    continue
                seen.add(url)
                page_count, discovered, page_notes = await _collect_x_web_page(db, client, source, url, request.limit - collected)
                pages_seen += 1
                collected += page_count
                notes.extend(page_notes)
                for discovered_url in discovered:
                    normalized = _normalize_x_url(discovered_url)
                    if normalized not in seen and normalized not in queue:
                        queue.append(normalized)
        run.status = "success"
        run.items_collected = collected
        run.finished_at = utcnow()
        note = None
        if notes:
            note = " ".join(dict.fromkeys(notes))
        elif collected == 0:
            note = "Fetched public X/Twitter pages, but no tweet objects were exposed in the logged-out page HTML."
        db.commit()
        return {
            "run_id": run.id,
            "status": run.status,
            "source_mode": "web",
            "items_collected": collected,
            "pages_seen": pages_seen,
            **({"note": note} if note else {}),
        }
    except Exception as exc:
        db.rollback()
        run = db.get(Run, run.id)
        if run:
            run.status = "failed"
            run.finished_at = utcnow()
            run.error = _friendly_x_error(exc)
            db.commit()
            return {"run_id": run.id, "status": run.status, "items_collected": run.items_collected, "error": run.error}
        raise


def _discover_x_status_urls_from_reddit(db: Session, request: XFromRedditRequest) -> list[str]:
    reddit_source = db.query(Source).filter(Source.name == "reddit").one_or_none()
    if not reddit_source:
        return []

    query = (
        db.query(Item)
        .filter(Item.source_id == reddit_source.id, Item.deleted_or_removed.is_(False))
        .order_by(Item.created_time.desc(), Item.collected_at.desc())
    )
    since = since_for_window(request.time_window)
    if since:
        query = query.filter(or_(Item.created_time.is_(None), Item.created_time >= since))

    keywords = [token.strip().lower().lstrip("@") for token in (request.query or "").split() if len(token.strip()) > 2]
    accounts = [account.strip().lower().lstrip("@") for account in (request.accounts or []) if account.strip()]
    urls: list[str] = []
    seen: set[str] = set()
    for item in query.limit(500).all():
        comments = db.query(Comment).filter(Comment.item_id == item.id).limit(20).all()
        haystack = " ".join(
            [
                item.title_or_text or "",
                item.self_text or "",
                item.url or "",
                item.permalink or "",
                " ".join(comment.body or "" for comment in comments),
            ]
        )
        haystack_lower = haystack.lower()
        if keywords and not any(keyword in haystack_lower for keyword in keywords):
            continue
        for url in _x_status_urls_from_text(haystack):
            handle = url.split("/")[3].lower()
            if accounts and handle not in accounts:
                continue
            if url not in seen:
                seen.add(url)
                urls.append(url)
                if len(urls) >= request.limit:
                    return urls
    return urls


async def collect_x_from_reddit(db: Session, request: XFromRedditRequest) -> dict[str, Any]:
    urls = _discover_x_status_urls_from_reddit(db, request)
    if not urls:
        run = Run(
            source="x",
            mode="reddit_status_discovery",
            status="success",
            items_collected=0,
            finished_at=utcnow(),
            raw_json=_json({"query": request.query, "accounts": request.accounts, "time_window": request.time_window}),
        )
        db.add(run)
        db.commit()
        return {
            "run_id": run.id,
            "status": run.status,
            "source_mode": "reddit_status_discovery",
            "items_collected": 0,
            "urls_discovered": 0,
            "note": "No X/Twitter status URLs were found in collected Reddit posts/comments for that request.",
        }

    result = await collect_x(
        db,
        XCollectRequest(
            urls=urls,
            source_mode="web",
            limit=max(10, min(100, len(urls))),
        ),
    )
    result["source_mode"] = "reddit_status_discovery"
    result["urls_discovered"] = len(urls)
    if result.get("items_collected", 0) == 0:
        result["note"] = result.get("note") or "Discovered X/Twitter status URLs from Reddit, but none exposed tweet objects in public HTML."
    return result


def _decoded_variants(text: str) -> list[str]:
    variants = [text, unescape(text)]
    current = variants[-1]
    for _ in range(3):
        decoded = unquote(current)
        if decoded == current:
            break
        variants.append(decoded)
        current = decoded
    return variants


def _extract_search_hrefs(html: str) -> list[str]:
    urls: list[str] = []
    for raw_href in HREF_RE.findall(html):
        href = unescape(raw_href)
        parsed = urlparse(href)
        if parsed.path == "/l/":
            target = parse_qs(parsed.query).get("uddg", [""])[0]
            if target:
                href = target
        elif parsed.path == "/url":
            query = parse_qs(parsed.query)
            target = (query.get("q") or query.get("url") or query.get("imgrefurl") or [""])[0]
            if target:
                href = target
        if href.startswith("//"):
            href = f"https:{href}"
        urls.append(href)
        decoded_bing = _decode_bing_redirect(href)
        if decoded_bing:
            urls.append(decoded_bing)
    return urls


def _decode_bing_redirect(url: str) -> str | None:
    parsed = urlparse(unescape(url))
    encoded = parse_qs(parsed.query).get("u", [""])[0]
    if not encoded:
        return None
    if encoded.startswith("a1"):
        encoded = encoded[2:]
    padding = "=" * (-len(encoded) % 4)
    try:
        return base64.urlsafe_b64decode(encoded + padding).decode("utf-8")
    except Exception:
        return None


def _clean_account(value: str | None) -> str | None:
    clean = (value or "").strip().lstrip("@")
    return clean or None


def _search_topic(request: XArchiveSearchRequest) -> str:
    return (request.topic or request.person or "").strip()


def _status_urls_from_decoded_text(text: str, account: str | None = None) -> list[str]:
    clean_account = (_clean_account(account) or "").lower()
    urls: list[str] = []
    seen: set[str] = set()
    for variant in _decoded_variants(text):
        for match in X_STATUS_RE.finditer(variant):
            if clean_account and match.group(1).lower() != clean_account:
                continue
            normalized = f"https://x.com/{match.group(1)}/status/{match.group(2)}"
            if normalized not in seen:
                seen.add(normalized)
                urls.append(normalized)
    return urls


def _free_web_search_queries(account: str | None, topic: str) -> list[str]:
    clean_account = _clean_account(account)
    clean_topic = topic.strip()
    if not clean_topic:
        return []
    if clean_account:
        return [
            f"site:x.com/{clean_account}/status {clean_topic}",
            f"site:twitter.com/{clean_account}/status {clean_topic}",
            f"site:x.com inurl:/status {clean_topic} {clean_account}",
        ]
    return [
        f"site:x.com inurl:/status {clean_topic} video OR pic.twitter.com",
        f"site:twitter.com inurl:/status {clean_topic} video OR pic.twitter.com",
        f"site:x.com inurl:/status {clean_topic}",
    ]


def _free_web_search_result_urls(query: str) -> list[str]:
    encoded = quote_plus(query)
    return [
        f"https://duckduckgo.com/html/?q={encoded}",
        f"https://r.jina.ai/http://r.jina.ai/http://duckduckgo.com/html/?q={encoded}",
        f"https://www.bing.com/search?q={encoded}",
        f"https://r.jina.ai/http://r.jina.ai/http://www.bing.com/search?q={encoded}",
    ]


def _upsert_search_video_lead(db: Session, source: Source, url: str, topic: str) -> tuple[Item, bool]:
    match = X_STATUS_RE.search(url)
    if not match:
        raise ValueError(f"Not an X/Twitter status URL: {url}")
    handle, status_id = match.group(1), match.group(2)
    normalized_url = f"https://x.com/{handle}/status/{status_id}"
    topic_note = f"Search topic: {topic.strip() or handle}. Search-discovered X/Twitter video lead."
    item = db.query(Item).filter(Item.source_id == source.id, Item.external_id == status_id).one_or_none()
    if item:
        if topic_note not in (item.self_text or ""):
            item.self_text = f"{item.self_text or ''}\n{topic_note}".strip()
            db.flush()
            rank_item(db, item)
        return item, False

    title = f"Search-discovered Twitter/X video lead for {topic.strip() or handle}"
    item = Item(
        source_id=source.id,
        external_id=status_id,
        title_or_text=title,
        author_name=handle,
        url=normalized_url,
        permalink=normalized_url,
        domain="x.com",
        self_text=topic_note,
        is_video=True,
        metrics_json=_json({"free_web_search_video_result": 1}),
        raw_json=_json(
            {
                "free_web_search_discovered": True,
                "discovery_topic": topic,
                "source_url": normalized_url,
                "note": "Created from free web search result because X/Twitter metadata was not exposed.",
            }
        ),
    )
    db.add(item)
    db.flush()
    rank_item(db, item)
    return item, True


async def _discover_x_status_urls_from_free_web_search(
    client: httpx.AsyncClient,
    account: str | None,
    topic: str,
    limit: int,
    urls: list[str],
    seen: set[str],
) -> tuple[list[str], int]:
    pages_seen = 0
    for query in _free_web_search_queries(account, topic):
        if len(urls) >= limit or pages_seen >= WEB_SEARCH_PAGE_LIMIT:
            break
        for search_url in _free_web_search_result_urls(query):
            if len(urls) >= limit or pages_seen >= WEB_SEARCH_PAGE_LIMIT:
                break
            try:
                response = await client.get(search_url)
                response.raise_for_status()
            except Exception:
                continue
            pages_seen += 1
            chunks = [response.text, *_extract_search_hrefs(response.text), *ARCHIVE_URL_RE.findall(response.text)]
            for chunk in chunks:
                for url in _status_urls_from_decoded_text(chunk, account):
                    if url not in seen:
                        seen.add(url)
                        urls.append(url)
                        if len(urls) >= limit:
                            break
                if len(urls) >= limit:
                    break
    return urls, pages_seen


def _archive_search_queries(account: str, person: str) -> list[str]:
    clean_account = account.strip().lstrip("@")
    clean_person = person.strip()
    quoted_status = f'"x.com/{clean_account}/status"'
    quoted_twitter_status = f'"twitter.com/{clean_account}/status"'
    queries: list[str] = []
    for domain in ARCHIVE_DOMAINS:
        queries.append(f"site:{domain} {quoted_status} {clean_person}")
        queries.append(f"site:{domain} {quoted_twitter_status} {clean_person}")
    queries.append(f'"x.com/{clean_account}/status" {clean_person}')
    return queries


def _search_result_urls(query: str) -> list[str]:
    encoded = quote_plus(query)
    return [
        f"https://duckduckgo.com/html/?q={encoded}",
        f"https://www.bing.com/search?q={encoded}",
        f"https://r.jina.ai/http://r.jina.ai/http://www.bing.com/search?q={encoded}",
    ]


def _archive_index_urls(account: str) -> list[str]:
    clean_account = account.strip().lstrip("@")
    return [
        f"https://archive.ph/offset=0/x.com/{clean_account}",
        f"https://archive.ph/offset=0/x.com/{clean_account}/with_replies",
        f"https://archive.ph/offset=0/twitter.com/{clean_account}",
        f"https://archive.ph/offset=0/twitter.com/{clean_account}/with_replies",
    ]


async def _discover_x_status_urls_from_archive_search(request: XArchiveSearchRequest) -> tuple[list[str], list[str], int]:
    account = _clean_account(request.account)
    topic = _search_topic(request)
    use_web = request.search_provider in {"web", "google", "all"}
    use_archive = request.search_provider in {"archive", "all"}
    urls: list[str] = []
    seen: set[str] = set()
    notes: list[str] = []
    pages_seen = 0

    async with httpx.AsyncClient(**web_client_kwargs(timeout=6.0, follow_redirects=True)) as client:
        if request.archive_html:
            for url in _status_urls_from_decoded_text(request.archive_html, account):
                if url not in seen:
                    seen.add(url)
                    urls.append(url)
                    if len(urls) >= request.limit:
                        break
            if urls:
                notes.append("Used pasted search/archive HTML to discover direct X status URLs.")

        for pasted_url in request.archive_urls:
            if len(urls) >= request.limit:
                break
            pasted_url = pasted_url.strip()
            if not pasted_url:
                continue
            chunks = [pasted_url]
            try:
                response = await client.get(pasted_url)
                if _archive_response_is_blocked(response):
                    notes.append("Archive page was blocked, so the app used any direct X status URL embedded in the pasted link.")
                else:
                    response.raise_for_status()
                    chunks.append(response.text)
                    pages_seen += 1
            except Exception as exc:
                notes.append(f"Archive/search page fetch failed for {pasted_url}: {_friendly_fetch_error(exc, 'Archive/search page')}")
            for chunk in chunks:
                for url in _status_urls_from_decoded_text(chunk, account):
                    if url not in seen:
                        seen.add(url)
                        urls.append(url)
                        if len(urls) >= request.limit:
                            break
                if len(urls) >= request.limit:
                    break

        if len(urls) >= request.limit or ((request.archive_urls or request.archive_html) and request.search_provider == "archive"):
            return urls, list(dict.fromkeys(notes)), pages_seen

        if use_web:
            if not topic:
                notes.append("Enter a person or topic so free web search can discover matching X/Twitter status URLs.")
            else:
                before = len(urls)
                urls, web_pages_seen = await _discover_x_status_urls_from_free_web_search(
                    client, account, topic, request.limit, urls, seen
                )
                pages_seen += web_pages_seen
                if len(urls) > before:
                    notes.append("Used free web search results to discover direct X/Twitter status URLs.")
                elif web_pages_seen == 0:
                    notes.append("Free web search did not return an accessible result page.")
            if request.search_provider in {"web", "google"} or len(urls) >= request.limit:
                return urls, list(dict.fromkeys(notes)), pages_seen

        if not use_archive:
            return urls, list(dict.fromkeys(notes)), pages_seen

        if not account or not topic:
            notes.append("Archive search needs both an X account and a person/topic.")
            return urls, list(dict.fromkeys(notes)), pages_seen

        for index_url in _archive_index_urls(account):
            if len(urls) >= request.limit:
                break
            try:
                response = await client.get(index_url)
                if _archive_response_is_blocked(response):
                    notes.append(_archive_blocked_note())
                    break
                response.raise_for_status()
            except Exception as exc:
                notes.append(f"Archive index fetch failed for {index_url}: {_friendly_fetch_error(exc, 'Archive.ph')}")
                continue
            pages_seen += 1
            haystack = response.text
            if topic.lower() not in unescape(haystack).lower():
                continue
            for url in _status_urls_from_decoded_text(haystack, account):
                if url not in seen:
                    seen.add(url)
                    urls.append(url)
                    if len(urls) >= request.limit:
                        break

        search_pages_seen = 0
        for query in _archive_search_queries(account, topic):
            if len(urls) >= request.limit:
                break
            for search_url in _search_result_urls(query):
                if len(urls) >= request.limit or search_pages_seen >= 9:
                    break
                try:
                    response = await client.get(search_url)
                    response.raise_for_status()
                except Exception:
                    continue
                pages_seen += 1
                search_pages_seen += 1
                chunks = [response.text, *_extract_search_hrefs(response.text), *ARCHIVE_URL_RE.findall(response.text)]
                for chunk in chunks:
                    for url in _status_urls_from_decoded_text(chunk, account):
                        if url not in seen:
                            seen.add(url)
                            urls.append(url)
                            if len(urls) >= request.limit:
                                break
                    if len(urls) >= request.limit:
                        break
                if len(urls) >= request.limit:
                    break
            if search_pages_seen >= 9:
                break

    return urls, list(dict.fromkeys(notes)), pages_seen


async def collect_x_from_archive_search(db: Session, request: XArchiveSearchRequest) -> dict[str, Any]:
    urls, notes, pages_seen = await _discover_x_status_urls_from_archive_search(request)
    topic = _search_topic(request)
    source_mode = (
        "web_search_discovery"
        if request.search_provider in {"web", "google"}
        else "archive_search_discovery"
        if request.search_provider == "archive"
        else "search_discovery"
    )
    if not urls:
        run = Run(
            source="x",
            mode=source_mode,
            status="success",
            items_collected=0,
            finished_at=utcnow(),
            raw_json=_json(
                {
                    "account": request.account,
                    "person": request.person,
                    "topic": request.topic,
                    "search_provider": request.search_provider,
                    "limit": request.limit,
                    "pages_seen": pages_seen,
                }
            ),
        )
        db.add(run)
        db.commit()
        if request.search_provider in {"web", "google"}:
            note = "No direct X/Twitter status URLs were found through free web search for that person/topic."
        elif request.search_provider == "archive":
            note = "No archived direct X status URLs were found for that account/person."
        else:
            note = "No direct X/Twitter status URLs were found for that search."
        if notes:
            note = f"{note} {' '.join(notes)}"
        return {
            "run_id": run.id,
            "status": run.status,
            "source_mode": source_mode,
            "items_collected": 0,
            "urls_discovered": 0,
            "pages_seen": pages_seen,
            "note": note,
        }

    result = await collect_x(
        db,
        XCollectRequest(
            urls=urls,
            source_mode="web",
            limit=max(10, min(100, len(urls))),
        ),
    )
    fallback_created = 0
    if request.search_provider in {"web", "google"}:
        source = _source(db)
        for url in urls:
            try:
                _item, created = _upsert_search_video_lead(db, source, url, topic)
            except ValueError:
                continue
            if created:
                fallback_created += 1
        db.commit()
        if fallback_created:
            run = db.get(Run, result.get("run_id"))
            if run:
                run.mode = source_mode
                run.items_collected = (run.items_collected or 0) + fallback_created
                if run.status == "failed":
                    run.status = "success"
                    run.error = None
                result["items_collected"] = run.items_collected
                result["status"] = run.status
            else:
                result["items_collected"] = (result.get("items_collected") or 0) + fallback_created
                if result.get("status") == "failed":
                    result["status"] = "success"
            db.commit()
    result["source_mode"] = source_mode
    result["urls_discovered"] = len(urls)
    result["web_search_video_leads_created"] = fallback_created
    result["discovered_urls"] = urls
    result["pages_seen"] = pages_seen
    if notes:
        existing_note = result.get("note")
        note_parts = [existing_note] if existing_note else []
        note_parts.extend(notes)
        result["note"] = " ".join(note_parts)
    if fallback_created:
        lead_note = (
            f"Added {fallback_created} search-discovered Twitter/X video lead"
            f"{'' if fallback_created == 1 else 's'} because X/Twitter metadata was not exposed."
        )
        result["note"] = f"{result.get('note', '')} {lead_note}".strip()
    return result


def _load_json_or_archive_js(path: Path) -> Any:
    text = path.read_text(encoding="utf-8")
    stripped = text.lstrip()
    if stripped.startswith(("[", "{")):
        return json.loads(stripped)
    first_array = text.find("[")
    first_object = text.find("{")
    starts = [index for index in (first_array, first_object) if index != -1]
    if not starts:
        raise ValueError(f"{path} does not contain JSON data")
    payload, _end = json.JSONDecoder().raw_decode(text[min(starts) :])
    return payload


def _archive_files(path: str) -> list[Path]:
    base = Path(path).expanduser()
    if base.is_file():
        return [base]
    if not base.is_dir():
        raise FileNotFoundError(f"X archive path was not found: {base}")
    candidates: list[Path] = []
    for folder in (base, base / "data"):
        if folder.is_dir():
            candidates.extend(folder.glob("tweets*.js"))
            candidates.extend(folder.glob("tweets*.json"))
    return sorted(dict.fromkeys(candidates))


def _archive_account(path: str) -> str | None:
    base = Path(path).expanduser()
    folders = [base.parent] if base.is_file() else [base, base / "data"]
    for folder in folders:
        for account_file in sorted(folder.glob("account*.js")) + sorted(folder.glob("account*.json")):
            try:
                payload = _load_json_or_archive_js(account_file)
            except Exception:
                continue
            rows = payload if isinstance(payload, list) else [payload]
            for row in rows:
                account = row.get("account") if isinstance(row, dict) else None
                if isinstance(account, dict):
                    username = account.get("username") or account.get("accountDisplayName")
                    if username:
                        return str(username).lstrip("@")
    return None


def _archive_tweets_from_payload(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        tweets: list[dict[str, Any]] = []
        for row in payload:
            if isinstance(row, dict) and isinstance(row.get("tweet"), dict):
                tweets.append(row["tweet"])
            elif isinstance(row, dict) and (row.get("id_str") or row.get("id")):
                tweets.append(row)
        return tweets
    if isinstance(payload, dict):
        if isinstance(payload.get("tweet"), dict):
            return [payload["tweet"]]
        if isinstance(payload.get("tweets"), list):
            return _archive_tweets_from_payload(payload["tweets"])
    return []


async def collect_x_archive(db: Session, request: XArchiveImportRequest) -> dict[str, Any]:
    clean_account = (request.account or _archive_account(request.path) or "").strip().lstrip("@") or None
    run = Run(
        source="x",
        mode="archive_import",
        status="running",
        raw_json=_json({"path": request.path, "account": clean_account, "limit": request.limit}),
    )
    db.add(run)
    db.commit()

    try:
        files = _archive_files(request.path)
        if not files:
            raise FileNotFoundError(f"No tweets*.js or tweets*.json files found in X archive path: {request.path}")

        source = _source(db)
        collected = 0
        files_read = 0
        for path in files:
            payload = _load_json_or_archive_js(path)
            files_read += 1
            for raw_tweet in _archive_tweets_from_payload(payload):
                tweet, users, media = _tweet_from_archive(raw_tweet, clean_account)
                if not tweet.get("id") or not tweet.get("text"):
                    continue
                _upsert_post(db, source, tweet, users, media)
                collected += 1
                if collected >= request.limit:
                    break
            if collected >= request.limit:
                break

        run.status = "success"
        run.items_collected = collected
        run.finished_at = utcnow()
        db.commit()
        note = None
        if collected == 0:
            note = "Read the X archive files, but did not find tweet entries with ids and text."
        return {
            "run_id": run.id,
            "status": run.status,
            "source_mode": "archive",
            "items_collected": collected,
            "files_read": files_read,
            **({"note": note} if note else {}),
        }
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


async def collect_x(db: Session, request: XCollectRequest) -> dict[str, Any]:
    settings = get_settings()
    use_api = request.source_mode == "api" or (request.source_mode == "auto" and settings.x_configured)
    run = Run(
        source="x",
        mode="recent_search" if use_api else "web",
        status="running",
        raw_json=_json({"query": request.query, "accounts": request.accounts, "urls": request.urls, "source_mode": request.source_mode}),
    )
    db.add(run)
    db.commit()

    if request.source_mode == "api" and not settings.x_configured:
        run.status = "failed"
        run.finished_at = utcnow()
        run.error = "X API bearer token is missing. Fill X_BEARER_TOKEN in .env."
        db.commit()
        return {"run_id": run.id, "status": run.status, "items_collected": 0, "error": run.error}

    if not use_api:
        return await _collect_x_web(db, request, run)

    try:
        params = {
            "query": _query(request),
            "max_results": request.limit,
            "expansions": "author_id,attachments.media_keys,referenced_tweets.id",
            "tweet.fields": "created_at,public_metrics,conversation_id,entities,attachments,referenced_tweets",
            "user.fields": "username,name,verified,public_metrics",
            "media.fields": "duration_ms,height,media_key,preview_image_url,public_metrics,type,url,width,variants",
        }
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(
                "https://api.x.com/2/tweets/search/recent",
                params=params,
                headers={"Authorization": f"Bearer {settings.x_bearer_token}"},
            )
            response.raise_for_status()
            payload = response.json()
            users = {user["id"]: user for user in payload.get("includes", {}).get("users", [])}
            media = {entry["media_key"]: entry for entry in payload.get("includes", {}).get("media", [])}
            source = _source(db)
            collected = 0
            for tweet in payload.get("data", []):
                _upsert_post(db, source, tweet, users, media)
                collected += 1
            run.status = "success"
            run.items_collected = collected
            run.finished_at = utcnow()
            db.commit()
            return {"run_id": run.id, "status": run.status, "source_mode": "api", "items_collected": collected}
    except Exception as exc:
        db.rollback()
        run = db.get(Run, run.id)
        if run:
            run.status = "failed"
            run.finished_at = utcnow()
            run.error = _friendly_x_error(exc)
            db.commit()
            return {"run_id": run.id, "status": run.status, "items_collected": run.items_collected, "error": run.error}
        raise
