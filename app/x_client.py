import json
from html import unescape
from datetime import datetime
from typing import Any
from urllib.parse import quote_plus

import httpx
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Item, Media, Run, Source, utcnow
from app.ranking import KEYWORDS, rank_item
from app.schemas import XCollectRequest
from app.web_headers import web_client_kwargs


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, default=str)


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


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
) -> tuple[int, list[str]]:
    response = await client.get(url)
    response.raise_for_status()
    state = _parse_x_state(response.text)
    raw_users = _state_entities(state, "users")
    users = _users_from_web(raw_users)
    collected = 0
    media: dict[str, dict[str, Any]] = {}
    for raw_tweet in _state_entities(state, "tweets").values():
        tweet, tweet_media = _tweet_from_web(raw_tweet)
        if not tweet.get("id") or not tweet.get("text"):
            continue
        media.update(tweet_media)
        _upsert_post(db, source, tweet, users, media)
        collected += 1
        if collected >= remaining:
            break

    pinned_urls: list[str] = []
    for user in raw_users.values():
        screen_name = user.get("screen_name") or user.get("username") or "i"
        for tweet_id in user.get("pinned_tweet_ids_str") or []:
            pinned_urls.append(f"https://x.com/{screen_name}/status/{tweet_id}")
    return collected, pinned_urls


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
    seen = set()
    queue = list(dict.fromkeys(seed_urls))
    try:
        async with httpx.AsyncClient(**web_client_kwargs(timeout=30.0, follow_redirects=True)) as client:
            while queue and collected < request.limit:
                url = queue.pop(0)
                if not url or url in seen:
                    continue
                seen.add(url)
                page_count, discovered = await _collect_x_web_page(db, client, source, url, request.limit - collected)
                pages_seen += 1
                collected += page_count
                for discovered_url in discovered:
                    normalized = _normalize_x_url(discovered_url)
                    if normalized not in seen and normalized not in queue:
                        queue.append(normalized)
        run.status = "success"
        run.items_collected = collected
        run.finished_at = utcnow()
        note = None
        if collected == 0:
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
            run.error = str(exc)
            db.commit()
            return {"run_id": run.id, "status": run.status, "items_collected": run.items_collected, "error": run.error}
        raise
