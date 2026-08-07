import asyncio
import html
import json
import mimetypes
import re
import shutil
import tempfile
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlparse

import httpx
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Item
from app.pinterest_client import download_pinterest_pin, pinterest_query_slug, search_public_pinterest
from app.schemas import LinksDownloadRequest, PinterestImageResearchRequest, XLinksDownloadRequest
from app.web_headers import web_client_kwargs
from app.x_client import _tweet_from_public_status_html

router = APIRouter()

DOWNLOAD_ROOT = Path("/data/downloads")
DOWNLOAD_TIMEOUT_SECONDS = 900
LINK_DOWNLOAD_CONCURRENCY = 2
MAX_IMAGE_BYTES = 30 * 1024 * 1024
X_STATUS_PATH_RE = re.compile(r"^/([A-Za-z0-9_]{1,20})/status(?:es)?/(\d+)(?:/.*)?$", re.IGNORECASE)
YOUTUBE_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{6,20}$")
REDDIT_POST_PATH_RE = re.compile(r"/comments/([A-Za-z0-9]+)/", re.IGNORECASE)
INSTAGRAM_MEDIA_PATH_RE = re.compile(
    r"^/(p|reel|reels|tv)/([A-Za-z0-9_-]{5,64})(?:/.*)?$",
    re.IGNORECASE,
)
TWITCH_CLIP_SLUG_RE = re.compile(r"^[A-Za-z0-9_-]{3,128}$")
TWITCH_CHANNEL_RE = re.compile(r"^[A-Za-z0-9_]{1,25}$")
KICK_CHANNEL_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
KICK_CLIP_ID_RE = re.compile(r"^clip_[A-Za-z0-9_-]{3,128}$", re.IGNORECASE)
KICK_VOD_ID_RE = re.compile(
    r"^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$",
    re.IGNORECASE,
)
RUMBLE_VIDEO_PATH_RE = re.compile(
    r"^/(?P<id>v(?!ideos(?:$|[-.]))[A-Za-z0-9]+)(?:[-.][^/]*)?/?$",
    re.IGNORECASE,
)
RUMBLE_EMBED_PATH_RE = re.compile(
    r"^/embed/(?:[0-9a-z]+\.)?(?P<id>[0-9a-z]+)/?$",
    re.IGNORECASE,
)
ARCTIC_SHIFT_POSTS_BY_ID_URL = "https://arctic-shift.photon-reddit.com/api/posts/ids"
X_HOSTS = {
    "x.com",
    "www.x.com",
    "mobile.x.com",
    "twitter.com",
    "www.twitter.com",
    "mobile.twitter.com",
}
YOUTUBE_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtube-nocookie.com",
    "www.youtube-nocookie.com",
    "youtu.be",
    "www.youtu.be",
}
REDDIT_HOSTS = {
    "reddit.com",
    "www.reddit.com",
    "old.reddit.com",
    "new.reddit.com",
    "sh.reddit.com",
    "redd.it",
    "www.redd.it",
    "v.redd.it",
}
INSTAGRAM_HOSTS = {
    "instagram.com",
    "www.instagram.com",
    "m.instagram.com",
}
TWITCH_HOSTS = {
    "twitch.tv",
    "www.twitch.tv",
    "m.twitch.tv",
    "go.twitch.tv",
    "clips.twitch.tv",
    "www.clips.twitch.tv",
}
KICK_HOSTS = {
    "kick.com",
    "www.kick.com",
}
RUMBLE_HOSTS = {
    "rumble.com",
    "www.rumble.com",
}
TWITCH_RESERVED_PATHS = {
    "creatorcamp",
    "directory",
    "downloads",
    "drops",
    "inventory",
    "jobs",
    "p",
    "search",
    "settings",
    "store",
    "subscriptions",
    "turbo",
    "videos",
    "wallet",
}
KICK_RESERVED_PATHS = {
    "auth",
    "browse",
    "categories",
    "clips",
    "communities",
    "dashboard",
    "following",
    "search",
    "video",
    "videos",
}
IMAGE_EXTENSIONS = {
    "image/gif": ".gif",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}


@dataclass
class DownloadProcessResult:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False


@dataclass
class XSnapshot:
    text: str
    handle: str
    display_name: str
    url: str
    status_id: str
    created_at: str | None = None
    metrics: dict[str, Any] | None = None
    image_urls: list[str] | None = None


@dataclass
class RedditSnapshot:
    title: str
    body: str
    author: str
    subreddit: str
    url: str
    post_id: str
    created_at: str | None = None
    score: int | None = None
    comment_count: int | None = None


def _safe_segment(value: str | None, fallback: str, *, max_length: int = 80, lowercase: bool = False) -> str:
    text = html.unescape(value or "")
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    if lowercase:
        text = text.lower()
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", text).strip(".-")
    cleaned = re.sub(r"-{2,}", "-", cleaned)
    return cleaned[:max_length].strip(".-") or fallback


def _load_json(value: str | None, fallback: Any) -> Any:
    try:
        return json.loads(value or "")
    except (json.JSONDecodeError, TypeError):
        return fallback


def _decode_html_entities(value: Any) -> str:
    decoded = str(value or "")
    for _attempt in range(3):
        unescaped = html.unescape(decoded)
        if unescaped == decoded:
            break
        decoded = unescaped
    return decoded


def _x_snapshot_from_item(item: Item) -> XSnapshot:
    try:
        normalized_url, url_handle, status_id = _normalize_x_status_url(item.url)
    except ValueError:
        normalized_url = item.url
        url_handle = item.author_name or "x-user"
        status_id = item.external_id

    raw = _load_json(item.raw_json, {})
    raw_author = raw.get("author") if isinstance(raw, dict) and isinstance(raw.get("author"), dict) else {}
    handle = str(raw_author.get("username") or item.author_name or url_handle).lstrip("@")
    display_name = _decode_html_entities(raw_author.get("name") or item.author_name or handle)
    image_urls: list[str] = []
    for entry in item.media:
        if entry.media_type not in {"photo", "image"}:
            continue
        image_url = entry.url or entry.preview_image_url
        if image_url:
            image_urls.append(image_url)
    return XSnapshot(
        text=_decode_html_entities(item.title_or_text),
        handle=handle,
        display_name=display_name,
        url=normalized_url,
        status_id=status_id,
        created_at=item.created_time.isoformat() if item.created_time else None,
        metrics=_load_json(item.metrics_json, {}),
        image_urls=list(dict.fromkeys(image_urls)),
    )


def _reddit_snapshot_from_item(item: Item) -> RedditSnapshot:
    raw = _load_json(item.raw_json, {})
    metrics = _load_json(item.metrics_json, {})
    permalink = item.permalink or item.url
    try:
        normalized_url, post_id = _normalize_reddit_url(permalink)
    except ValueError:
        normalized_url = permalink
        post_id = item.external_id
    return RedditSnapshot(
        title=_decode_html_entities(item.title_or_text),
        body=_decode_html_entities(item.self_text or (raw.get("selftext") if isinstance(raw, dict) else "")),
        author=str(item.author_name or (raw.get("author") if isinstance(raw, dict) else "") or "unknown"),
        subreddit=str(item.subreddit or (raw.get("subreddit") if isinstance(raw, dict) else "") or "reddit"),
        url=normalized_url,
        post_id=post_id,
        created_at=item.created_time.isoformat() if item.created_time else None,
        score=metrics.get("score") if isinstance(metrics.get("score"), int) else None,
        comment_count=metrics.get("num_comments") if isinstance(metrics.get("num_comments"), int) else None,
    )


async def _fetch_reddit_snapshot(url: str) -> RedditSnapshot:
    normalized_url, post_id = _normalize_reddit_url(url)
    json_url = f"https://www.reddit.com/comments/{post_id}.json"
    async with httpx.AsyncClient(**web_client_kwargs(timeout=30.0, follow_redirects=True)) as client:
        try:
            response = await client.get(json_url, params={"raw_json": "1", "limit": "1"})
            response.raise_for_status()
            payload = response.json()
            post = payload[0]["data"]["children"][0]["data"]
            return _reddit_snapshot_from_post_data(post, normalized_url, post_id)
        except (httpx.HTTPError, IndexError, KeyError, TypeError, ValueError):
            pass

        try:
            response = await client.get(
                ARCTIC_SHIFT_POSTS_BY_ID_URL,
                params={"ids": post_id},
            )
            response.raise_for_status()
            post = response.json()["data"][0]
            return _reddit_snapshot_from_post_data(post, normalized_url, post_id)
        except (httpx.HTTPError, IndexError, KeyError, TypeError, ValueError):
            pass

        response = await client.get(
            "https://www.reddit.com/oembed",
            params={"url": normalized_url},
        )
        response.raise_for_status()
        payload = response.json()

    title = _decode_html_entities(payload.get("title")).strip()
    if not title:
        raise ValueError("Reddit did not expose enough public post data to create a screenshot")
    embed_html = _decode_html_entities(payload.get("html"))
    subreddit_match = re.search(r"https?://(?:www\.)?reddit\.com/r/([^/\"']+)", embed_html, re.IGNORECASE)
    if not subreddit_match:
        subreddit_match = re.search(r"/r/([^/]+)", urlparse(normalized_url).path, re.IGNORECASE)
    return RedditSnapshot(
        title=title,
        body="",
        author=str(payload.get("author_name") or "unknown"),
        subreddit=subreddit_match.group(1) if subreddit_match else "reddit",
        url=normalized_url,
        post_id=post_id,
    )


def _reddit_snapshot_from_post_data(
    post: dict[str, Any],
    normalized_url: str,
    post_id: str,
) -> RedditSnapshot:
    title = _decode_html_entities(post.get("title")).strip()
    if not title:
        raise ValueError("Reddit post data did not include a title")
    permalink = post.get("permalink")
    snapshot_url = f"https://www.reddit.com{permalink}" if permalink else normalized_url
    created_at = None
    if isinstance(post.get("created_utc"), (int, float)):
        created_at = datetime.fromtimestamp(post["created_utc"], tz=timezone.utc).isoformat()
    return RedditSnapshot(
        title=title,
        body=_decode_html_entities(post.get("selftext")),
        author=str(post.get("author") or "unknown"),
        subreddit=str(post.get("subreddit") or "reddit"),
        url=snapshot_url,
        post_id=str(post.get("id") or post_id),
        created_at=created_at,
        score=post.get("score") if isinstance(post.get("score"), int) else None,
        comment_count=post.get("num_comments") if isinstance(post.get("num_comments"), int) else None,
    )


async def _fetch_x_snapshot(url: str) -> XSnapshot:
    normalized_url, url_handle, status_id = _normalize_x_status_url(url)
    async with httpx.AsyncClient(**web_client_kwargs(timeout=30.0, follow_redirects=True)) as client:
        response = await client.get(normalized_url)
        response.raise_for_status()
    parsed = _tweet_from_public_status_html(response.text, normalized_url)
    if not parsed:
        raise ValueError("X did not expose enough public post data to create a screenshot")
    tweet, users, media = parsed
    author = next(iter(users.values()), {})
    handle = str(author.get("username") or url_handle).lstrip("@")
    display_name = _decode_html_entities(author.get("name") or handle)
    image_urls = [
        str(entry.get("url") or entry.get("preview_image_url"))
        for entry in media.values()
        if entry.get("type") in {"photo", "image"} and (entry.get("url") or entry.get("preview_image_url"))
    ]
    public_html = response.text.replace("\\/", "/").replace("\\u0026", "&")
    image_urls.extend(
        _decode_html_entities(candidate).rstrip(".,)")
        for candidate in re.findall(
            r"https?://pbs\.twimg\.com/media/[^\"'<>\s]+",
            public_html,
            re.IGNORECASE,
        )
    )
    return XSnapshot(
        text=_decode_html_entities(tweet.get("text")),
        handle=handle,
        display_name=display_name,
        url=normalized_url,
        status_id=status_id,
        created_at=tweet.get("created_at"),
        metrics=tweet.get("public_metrics") if isinstance(tweet.get("public_metrics"), dict) else {},
        image_urls=list(dict.fromkeys(image_urls)),
    )


def _image_extension(content_type: str, url: str) -> str:
    media_type = content_type.split(";", 1)[0].strip().lower()
    if media_type in IMAGE_EXTENSIONS:
        return IMAGE_EXTENSIONS[media_type]
    suffix = Path(urlparse(url).path).suffix.lower()
    if suffix in {".gif", ".jpeg", ".jpg", ".png", ".webp"}:
        return ".jpg" if suffix == ".jpeg" else suffix
    guessed = mimetypes.guess_extension(media_type)
    if guessed in {".gif", ".jpeg", ".jpg", ".png", ".webp"}:
        return ".jpg" if guessed == ".jpeg" else guessed
    raise ValueError(f"URL did not return a supported image ({media_type or 'unknown content type'})")


async def _download_image_url(url: str, destination_stem: Path) -> Path:
    host = (urlparse(url).hostname or "").lower()
    if host not in {"pbs.twimg.com", "video.twimg.com"} and not host.endswith(".twimg.com"):
        raise ValueError("Refusing an image URL outside X/Twitter media hosts")
    async with httpx.AsyncClient(**web_client_kwargs(timeout=60.0, follow_redirects=True)) as client:
        response = await client.get(url, headers={"Accept": "image/avif,image/webp,image/png,image/jpeg,image/gif,*/*;q=0.8"})
        response.raise_for_status()
    if len(response.content) > MAX_IMAGE_BYTES:
        raise ValueError(f"Image is larger than the {MAX_IMAGE_BYTES // (1024 * 1024)} MB limit")
    extension = _image_extension(response.headers.get("content-type", ""), str(response.url))
    destination = destination_stem.with_suffix(extension)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(f"{extension}.part")
    temporary.write_bytes(response.content)
    temporary.replace(destination)
    return destination


def _tweet_font(size: int, *, bold: bool = False):
    from PIL import ImageFont

    names = (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    ) if bold else (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
    )
    for name in names:
        if Path(name).exists():
            return ImageFont.truetype(name, size)
    return ImageFont.load_default(size=size)


def _wrap_tweet_text(draw, value: str, font, max_width: int) -> list[str]:
    def split_wide_word(word: str) -> list[str]:
        chunks: list[str] = []
        current = ""
        for character in word:
            candidate = f"{current}{character}"
            if current and draw.textlength(candidate, font=font) > max_width:
                chunks.append(current)
                current = character
            else:
                current = candidate
        if current:
            chunks.append(current)
        return chunks or [word]

    lines: list[str] = []
    for paragraph in (value or "").replace("\r", "").split("\n"):
        words = [
            chunk
            for word in paragraph.split()
            for chunk in (
                split_wide_word(word)
                if draw.textlength(word, font=font) > max_width
                else [word]
            )
        ]
        if not words:
            lines.append("")
            continue
        current = words[0]
        for word in words[1:]:
            candidate = f"{current} {word}"
            if draw.textlength(candidate, font=font) <= max_width:
                current = candidate
            else:
                lines.append(current)
                current = word
        lines.append(current)
    return lines


def _fit_text(draw, value: str, font, max_width: int) -> str:
    if draw.textlength(value, font=font) <= max_width:
        return value
    suffix = "..."
    low, high = 0, len(value)
    while low < high:
        middle = (low + high + 1) // 2
        if draw.textlength(f"{value[:middle]}{suffix}", font=font) <= max_width:
            low = middle
        else:
            high = middle - 1
    return f"{value[:low]}{suffix}"


def _metric_text(metrics: dict[str, Any] | None) -> str:
    values = metrics or {}
    parts = []
    for key, label in (
        ("reply_count", "replies"),
        ("retweet_count", "reposts"),
        ("like_count", "likes"),
        ("quote_count", "quotes"),
    ):
        value = values.get(key)
        if isinstance(value, (int, float)):
            parts.append(f"{int(value):,} {label}")
    return "   ·   ".join(parts)


def _display_timestamp(value: str | None) -> str:
    if not value:
        return ""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.strftime("%-I:%M %p · %b %-d, %Y")
    except (ValueError, TypeError):
        return value


def _render_tweet_screenshot(snapshot: XSnapshot, destination: Path) -> Path:
    from PIL import Image, ImageDraw

    canvas_width = 1200
    card_width = 1040
    horizontal_padding = 72
    body_font = _tweet_font(42)
    name_font = _tweet_font(34, bold=True)
    handle_font = _tweet_font(29)
    meta_font = _tweet_font(27)
    footer_font = _tweet_font(22)
    scratch = Image.new("RGB", (canvas_width, 200), "white")
    scratch_draw = ImageDraw.Draw(scratch)
    clean_text = (snapshot.text or "").strip()
    if len(clean_text) > 3000:
        clean_text = f"{clean_text[:2997]}..."
    wrapped = _wrap_tweet_text(scratch_draw, clean_text, body_font, card_width - (horizontal_padding * 2))
    line_height = 56
    body_height = max(line_height, len(wrapped) * line_height)
    metrics_text = _metric_text(snapshot.metrics)
    card_height = 245 + body_height + (55 if metrics_text else 0) + 120
    canvas_height = card_height + 160

    image = Image.new("RGB", (canvas_width, canvas_height), "#f3f5f7")
    draw = ImageDraw.Draw(image)
    card_left, card_top = 80, 80
    card_right, card_bottom = card_left + card_width, card_top + card_height
    draw.rounded_rectangle((card_left, card_top, card_right, card_bottom), radius=32, fill="white", outline="#cfd9de", width=2)

    avatar_box = (card_left + horizontal_padding, card_top + 62, card_left + horizontal_padding + 94, card_top + 156)
    draw.ellipse(avatar_box, fill="#0f1419")
    initial = (snapshot.display_name or snapshot.handle or "X")[0].upper()
    initial_font = _tweet_font(45, bold=True)
    initial_box = draw.textbbox((0, 0), initial, font=initial_font)
    draw.text(
        (
            avatar_box[0] + (94 - (initial_box[2] - initial_box[0])) / 2,
            avatar_box[1] + (94 - (initial_box[3] - initial_box[1])) / 2 - 5,
        ),
        initial,
        font=initial_font,
        fill="white",
    )
    name_x = avatar_box[2] + 28
    draw.text((name_x, card_top + 64), snapshot.display_name or snapshot.handle, font=name_font, fill="#0f1419")
    draw.text((name_x, card_top + 112), f"@{snapshot.handle}", font=handle_font, fill="#536471")
    draw.text((card_right - 72, card_top + 70), "X", font=_tweet_font(42, bold=True), fill="#0f1419", anchor="ra")

    body_top = card_top + 195
    draw.multiline_text(
        (card_left + horizontal_padding, body_top),
        "\n".join(wrapped) or "(Post text unavailable)",
        font=body_font,
        fill="#0f1419",
        spacing=line_height - body_font.size,
    )
    meta_y = body_top + body_height + 32
    timestamp = _display_timestamp(snapshot.created_at)
    if timestamp:
        draw.text((card_left + horizontal_padding, meta_y), timestamp, font=meta_font, fill="#536471")
        meta_y += 54
    if metrics_text:
        draw.line((card_left + horizontal_padding, meta_y, card_right - horizontal_padding, meta_y), fill="#eff3f4", width=2)
        draw.text((card_left + horizontal_padding, meta_y + 24), metrics_text, font=meta_font, fill="#536471")
        meta_y += 70
    draw.line((card_left + horizontal_padding, card_bottom - 72, card_right - horizontal_padding, card_bottom - 72), fill="#eff3f4", width=2)
    draw.text(
        (card_left + horizontal_padding, card_bottom - 48),
        _fit_text(draw, snapshot.url, footer_font, card_width - (horizontal_padding * 2)),
        font=footer_font,
        fill="#536471",
        anchor="lm",
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    image.save(destination, format="PNG", optimize=True)
    return destination


def _render_reddit_screenshot(snapshot: RedditSnapshot, destination: Path) -> Path:
    from PIL import Image, ImageDraw

    canvas_width = 1200
    card_width = 1040
    horizontal_padding = 72
    title_font = _tweet_font(40, bold=True)
    body_font = _tweet_font(32)
    meta_font = _tweet_font(25)
    footer_font = _tweet_font(21)
    scratch = Image.new("RGB", (canvas_width, 200), "white")
    scratch_draw = ImageDraw.Draw(scratch)

    clean_title = (snapshot.title or "(Untitled Reddit post)").strip()
    clean_body = (snapshot.body or "").strip()
    if len(clean_title) > 600:
        clean_title = f"{clean_title[:597]}..."
    if len(clean_body) > 5000:
        clean_body = f"{clean_body[:4997]}..."
    title_lines = _wrap_tweet_text(
        scratch_draw,
        clean_title,
        title_font,
        card_width - (horizontal_padding * 2),
    )
    body_lines = _wrap_tweet_text(
        scratch_draw,
        clean_body,
        body_font,
        card_width - (horizontal_padding * 2),
    ) if clean_body else []
    title_line_height = 53
    body_line_height = 44
    title_height = max(title_line_height, len(title_lines) * title_line_height)
    body_height = len(body_lines) * body_line_height
    card_height = 250 + title_height + body_height + 145
    canvas_height = card_height + 160

    image = Image.new("RGB", (canvas_width, canvas_height), "#dae0e6")
    draw = ImageDraw.Draw(image)
    card_left, card_top = 80, 80
    card_right, card_bottom = card_left + card_width, card_top + card_height
    draw.rounded_rectangle(
        (card_left, card_top, card_right, card_bottom),
        radius=24,
        fill="white",
        outline="#cccccc",
        width=2,
    )
    draw.rounded_rectangle(
        (card_left, card_top, card_left + 18, card_bottom),
        radius=9,
        fill="#ff4500",
    )

    header_y = card_top + 58
    draw.ellipse(
        (card_left + horizontal_padding, header_y, card_left + horizontal_padding + 54, header_y + 54),
        fill="#ff4500",
    )
    draw.text(
        (card_left + horizontal_padding + 27, header_y + 27),
        "r/",
        font=_tweet_font(18, bold=True),
        fill="white",
        anchor="mm",
    )
    draw.text(
        (card_left + horizontal_padding + 72, header_y + 2),
        f"r/{snapshot.subreddit}",
        font=_tweet_font(27, bold=True),
        fill="#1a1a1b",
    )
    draw.text(
        (card_left + horizontal_padding + 72, header_y + 34),
        f"Posted by u/{snapshot.author}",
        font=meta_font,
        fill="#787c7e",
    )
    draw.text(
        (card_right - horizontal_padding, header_y + 22),
        "reddit",
        font=_tweet_font(27, bold=True),
        fill="#ff4500",
        anchor="rm",
    )

    title_y = card_top + 155
    draw.multiline_text(
        (card_left + horizontal_padding, title_y),
        "\n".join(title_lines),
        font=title_font,
        fill="#1a1a1b",
        spacing=title_line_height - title_font.size,
    )
    body_y = title_y + title_height + 30
    if body_lines:
        draw.multiline_text(
            (card_left + horizontal_padding, body_y),
            "\n".join(body_lines),
            font=body_font,
            fill="#1a1a1b",
            spacing=body_line_height - body_font.size,
        )

    metrics: list[str] = []
    if snapshot.score is not None:
        metrics.append(f"{snapshot.score:,} points")
    if snapshot.comment_count is not None:
        metrics.append(f"{snapshot.comment_count:,} comments")
    metrics_y = body_y + body_height + 38
    if snapshot.created_at:
        timestamp = _display_timestamp(snapshot.created_at)
        if timestamp:
            metrics.append(timestamp)
    if metrics:
        draw.text(
            (card_left + horizontal_padding, metrics_y),
            "   ·   ".join(metrics),
            font=meta_font,
            fill="#787c7e",
        )
    draw.line(
        (card_left + horizontal_padding, card_bottom - 72, card_right - horizontal_padding, card_bottom - 72),
        fill="#edeff1",
        width=2,
    )
    draw.text(
        (card_left + horizontal_padding, card_bottom - 46),
        _fit_text(draw, snapshot.url, footer_font, card_width - (horizontal_padding * 2)),
        font=footer_font,
        fill="#787c7e",
        anchor="lm",
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    image.save(destination, format="PNG", optimize=True)
    return destination


def _download_dir_for_item(item: Item, download_root: Path = DOWNLOAD_ROOT) -> Path:
    source = _safe_segment(item.source.name if item.source else None, "unknown")
    title_slug = _safe_segment(item.title_or_text, item.external_id or "clip", max_length=70, lowercase=True)
    return download_root / source / f"{item.id}-{title_slug}"


def _tail(value: str, limit: int = 4000) -> str:
    return value[-limit:] if len(value) > limit else value


def _file_info(path: Path, download_root: Path = DOWNLOAD_ROOT) -> dict[str, Any]:
    try:
        relative_path = path.relative_to(download_root)
    except ValueError:
        relative_path = path
    return {
        "path": str(path),
        "host_path": str(Path("./data/downloads") / relative_path),
        "download_url": f"/downloads/files/{quote(relative_path.as_posix(), safe='/')}",
        "name": path.name,
        "size_bytes": path.stat().st_size,
    }


def _resolve_download_file(file_path: str, download_root: Path = DOWNLOAD_ROOT) -> Path:
    root = download_root.resolve()
    try:
        candidate = (root / file_path).resolve(strict=True)
        candidate.relative_to(root)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=404, detail="Downloaded file not found") from exc
    if not candidate.is_file():
        raise HTTPException(status_code=404, detail="Downloaded file not found")
    return candidate


def _downloaded_files(target_dir: Path, download_root: Path = DOWNLOAD_ROOT) -> list[dict[str, Any]]:
    if not target_dir.exists():
        return []
    files = [path for path in target_dir.rglob("*") if path.is_file()]
    files.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    return [_file_info(path, download_root) for path in files[:20]]


def _download_urls(item: Item) -> list[str]:
    candidates = [item.url]
    if item.source and item.source.name == "kiwifarms":
        candidates.extend(entry.url for entry in item.media if entry.url)
    return list(dict.fromkeys(url for url in candidates if url.startswith(("http://", "https://"))))


def _normalize_x_status_url(value: str) -> tuple[str, str, str]:
    raw = value.strip()
    if not raw:
        raise ValueError("Empty URL")
    if len(raw) > 2048:
        raise ValueError("URL is too long")
    if not raw.startswith(("http://", "https://")):
        raw = f"https://{raw.lstrip('/')}"
    parsed = urlparse(raw)
    if parsed.scheme not in {"http", "https"} or (parsed.hostname or "").lower() not in X_HOSTS:
        raise ValueError("Only x.com or twitter.com links are supported")
    match = X_STATUS_PATH_RE.match(parsed.path)
    if not match:
        raise ValueError("URL must be a direct X/Twitter status link")
    handle, status_id = match.group(1).lower(), match.group(2)
    return f"https://x.com/{handle}/status/{status_id}", handle, status_id


def _normalize_youtube_url(value: str) -> tuple[str, str]:
    raw = value.strip()
    if not raw:
        raise ValueError("Empty URL")
    if len(raw) > 2048:
        raise ValueError("URL is too long")
    if not raw.startswith(("http://", "https://")):
        raw = f"https://{raw.lstrip('/')}"
    parsed = urlparse(raw)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or host not in YOUTUBE_HOSTS:
        raise ValueError("URL is not a supported YouTube link")

    video_id = None
    if host in {"youtu.be", "www.youtu.be"}:
        video_id = parsed.path.strip("/").split("/", 1)[0]
    elif parsed.path.rstrip("/") == "/watch":
        video_id = parse_qs(parsed.query).get("v", [None])[0]
    else:
        match = re.match(r"^/(?:shorts|live|embed)/([^/?#]+)", parsed.path, re.IGNORECASE)
        if match:
            video_id = match.group(1)
    if not video_id or not YOUTUBE_VIDEO_ID_RE.fullmatch(video_id):
        raise ValueError("URL must point to a YouTube video, Short, or live video")
    return f"https://www.youtube.com/watch?v={video_id}", video_id


def _normalize_reddit_url(value: str) -> tuple[str, str]:
    raw = value.strip()
    if not raw:
        raise ValueError("Empty URL")
    if len(raw) > 2048:
        raise ValueError("URL is too long")
    if not raw.startswith(("http://", "https://")):
        raw = f"https://{raw.lstrip('/')}"
    parsed = urlparse(raw)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or host not in REDDIT_HOSTS:
        raise ValueError("URL is not a supported Reddit link")

    path = "/" + parsed.path.strip("/")
    if host in {"v.redd.it", "redd.it", "www.redd.it"}:
        link_id = path.strip("/").split("/", 1)[0]
        if not re.fullmatch(r"[A-Za-z0-9]+", link_id or ""):
            raise ValueError("URL must point to a Reddit post or hosted video")
        canonical_host = "v.redd.it" if host == "v.redd.it" else "redd.it"
        return f"https://{canonical_host}/{link_id}", link_id.lower()

    match = REDDIT_POST_PATH_RE.search(f"{path}/")
    if not match:
        raise ValueError("URL must point to a Reddit post")
    post_id = match.group(1).lower()
    return f"https://www.reddit.com{path}", post_id


def _normalize_instagram_url(value: str) -> tuple[str, str]:
    raw = value.strip()
    if not raw:
        raise ValueError("Empty URL")
    if len(raw) > 2048:
        raise ValueError("URL is too long")
    if not raw.startswith(("http://", "https://")):
        raw = f"https://{raw.lstrip('/')}"
    parsed = urlparse(raw)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or host not in INSTAGRAM_HOSTS:
        raise ValueError("URL is not a supported Instagram link")

    match = INSTAGRAM_MEDIA_PATH_RE.fullmatch(parsed.path)
    if not match:
        raise ValueError("URL must point to an Instagram post, Reel, or IGTV video")
    media_kind, shortcode = match.groups()
    canonical_kind = "reel" if media_kind.lower() in {"reel", "reels"} else media_kind.lower()
    return f"https://www.instagram.com/{canonical_kind}/{shortcode}/", shortcode


def _normalize_twitch_url(value: str) -> tuple[str, str]:
    raw = value.strip()
    if not raw:
        raise ValueError("Empty URL")
    if len(raw) > 2048:
        raise ValueError("URL is too long")
    if not raw.startswith(("http://", "https://")):
        raw = f"https://{raw.lstrip('/')}"
    parsed = urlparse(raw)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or host not in TWITCH_HOSTS:
        raise ValueError("URL is not a supported Twitch link")

    path_parts = [part for part in parsed.path.split("/") if part]
    if host in {"clips.twitch.tv", "www.clips.twitch.tv"}:
        if len(path_parts) != 1 or not TWITCH_CLIP_SLUG_RE.fullmatch(path_parts[0]):
            raise ValueError("URL must point to a Twitch clip")
        clip_slug = path_parts[0]
        return f"https://clips.twitch.tv/{clip_slug}", f"clip-{clip_slug.lower()}"

    if len(path_parts) == 3 and path_parts[1].lower() == "clip":
        channel, _clip_path, clip_slug = path_parts
        if not TWITCH_CHANNEL_RE.fullmatch(channel) or not TWITCH_CLIP_SLUG_RE.fullmatch(clip_slug):
            raise ValueError("URL must point to a Twitch clip")
        return f"https://clips.twitch.tv/{clip_slug}", f"clip-{clip_slug.lower()}"

    if len(path_parts) == 2 and path_parts[0].lower() == "videos" and path_parts[1].isdigit():
        video_id = path_parts[1]
        return f"https://www.twitch.tv/videos/{video_id}", f"video-{video_id}"

    if len(path_parts) == 1:
        channel = path_parts[0].lower()
        if TWITCH_CHANNEL_RE.fullmatch(channel) and channel not in TWITCH_RESERVED_PATHS:
            return f"https://www.twitch.tv/{channel}", f"channel-{channel}"

    raise ValueError("URL must point to a Twitch clip, VOD, or live channel")


def _normalize_kick_url(value: str) -> tuple[str, str]:
    raw = value.strip()
    if not raw:
        raise ValueError("Empty URL")
    if len(raw) > 2048:
        raise ValueError("URL is too long")
    if not raw.startswith(("http://", "https://")):
        raw = f"https://{raw.lstrip('/')}"
    parsed = urlparse(raw)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or host not in KICK_HOSTS:
        raise ValueError("URL is not a supported Kick link")

    path_parts = [part for part in parsed.path.split("/") if part]
    if not path_parts or not KICK_CHANNEL_RE.fullmatch(path_parts[0]):
        raise ValueError("URL must point to a Kick clip, VOD, or live channel")
    channel = path_parts[0].lower()
    if channel in KICK_RESERVED_PATHS:
        raise ValueError("URL must point to a Kick clip, VOD, or live channel")

    query_clip_id = parse_qs(parsed.query).get("clip", [None])[0]
    if len(path_parts) == 1 and query_clip_id:
        if not KICK_CLIP_ID_RE.fullmatch(query_clip_id):
            raise ValueError("URL must point to a Kick clip")
        return (
            f"https://kick.com/{channel}/clips/{query_clip_id}",
            f"clip-{query_clip_id.lower()}",
        )

    if len(path_parts) == 3 and path_parts[1].lower() == "clips":
        clip_id = path_parts[2]
        if not KICK_CLIP_ID_RE.fullmatch(clip_id):
            raise ValueError("URL must point to a Kick clip")
        return f"https://kick.com/{channel}/clips/{clip_id}", f"clip-{clip_id.lower()}"

    if len(path_parts) == 3 and path_parts[1].lower() == "videos":
        vod_id = path_parts[2].lower()
        if not KICK_VOD_ID_RE.fullmatch(vod_id):
            raise ValueError("URL must point to a Kick VOD")
        return f"https://kick.com/{channel}/videos/{vod_id}", f"video-{vod_id}"

    if len(path_parts) == 1:
        return f"https://kick.com/{channel}", f"channel-{channel}"

    raise ValueError("URL must point to a Kick clip, VOD, or live channel")


def _normalize_rumble_url(value: str) -> tuple[str, str]:
    raw = value.strip()
    if not raw:
        raise ValueError("Empty URL")
    if len(raw) > 2048:
        raise ValueError("URL is too long")
    if not raw.startswith(("http://", "https://")):
        raw = f"https://{raw.lstrip('/')}"
    parsed = urlparse(raw)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or host not in RUMBLE_HOSTS:
        raise ValueError("URL is not a supported Rumble link")

    embed_match = RUMBLE_EMBED_PATH_RE.fullmatch(parsed.path)
    if embed_match:
        embed_id = embed_match.group("id").lower()
        return f"https://rumble.com/embed/{embed_id}", f"embed-{embed_id}"

    video_match = RUMBLE_VIDEO_PATH_RE.fullmatch(parsed.path)
    if video_match:
        video_id = video_match.group("id").lower()
        canonical_path = parsed.path.rstrip("/")
        return f"https://rumble.com{canonical_path}", f"video-{video_id}"

    raise ValueError("URL must point to a Rumble video or livestream")


def _normalize_download_url(value: str) -> tuple[str, str, str]:
    raw = value.strip()
    candidate = raw if raw.startswith(("http://", "https://")) else f"https://{raw.lstrip('/')}"
    host = (urlparse(candidate).hostname or "").lower()
    if host in X_HOSTS:
        normalized_url, handle, status_id = _normalize_x_status_url(raw)
        return normalized_url, "x", f"{handle}-{status_id}"
    if host in YOUTUBE_HOSTS:
        normalized_url, video_id = _normalize_youtube_url(raw)
        return normalized_url, "youtube", video_id
    if host in REDDIT_HOSTS:
        normalized_url, post_id = _normalize_reddit_url(raw)
        return normalized_url, "reddit", post_id
    if host in INSTAGRAM_HOSTS:
        normalized_url, shortcode = _normalize_instagram_url(raw)
        return normalized_url, "instagram", shortcode
    if host in TWITCH_HOSTS:
        normalized_url, twitch_key = _normalize_twitch_url(raw)
        return normalized_url, "twitch", twitch_key
    if host in KICK_HOSTS:
        normalized_url, kick_key = _normalize_kick_url(raw)
        return normalized_url, "kick", kick_key
    if host in RUMBLE_HOSTS:
        normalized_url, rumble_key = _normalize_rumble_url(raw)
        return normalized_url, "rumble", rumble_key
    raise ValueError(
        "Only X/Twitter, YouTube, Reddit, Instagram, Twitch, Kick, and Rumble media links are supported"
    )


def _download_dir_for_link(download_root: Path = DOWNLOAD_ROOT) -> Path:
    return download_root / "link-downloader"


def _link_work_dir(source: str, link_key: str, download_root: Path = DOWNLOAD_ROOT) -> Path:
    work_root = download_root / ".link-downloader-work"
    work_root.mkdir(parents=True, exist_ok=True)
    safe_source = _safe_segment(source, "source", max_length=20, lowercase=True)
    safe_key = _safe_segment(link_key, "video", max_length=50)
    return Path(tempfile.mkdtemp(prefix=f"{safe_source}-{safe_key}-", dir=work_root))


def _move_completed_files(
    work_dir: Path,
    target_dir: Path,
    download_root: Path = DOWNLOAD_ROOT,
) -> list[dict[str, Any]]:
    target_dir.mkdir(parents=True, exist_ok=True)
    completed = [
        path
        for path in work_dir.rglob("*")
        if path.is_file() and path.suffix.lower() not in {".part", ".ytdl"}
    ]
    moved: list[Path] = []
    try:
        for path in completed:
            destination = target_dir / path.name
            path.replace(destination)
            moved.append(destination)
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
    return [_file_info(path, download_root) for path in moved if path.exists()]


async def _save_x_snapshot_assets(
    snapshot: XSnapshot,
    target_dir: Path,
    *,
    filename_prefix: str = "",
) -> list[dict[str, Any]]:
    attempts: list[dict[str, Any]] = []
    for index, image_url in enumerate(snapshot.image_urls or [], start=1):
        try:
            path = await _download_image_url(image_url, target_dir / f"{filename_prefix}photo-{index}")
            attempts.append(
                {
                    "kind": "image",
                    "url": image_url,
                    "path": str(path),
                    "returncode": 0,
                    "timed_out": False,
                }
            )
        except Exception as exc:
            attempts.append(
                {
                    "kind": "image",
                    "url": image_url,
                    "returncode": 1,
                    "timed_out": False,
                    "error": str(exc),
                }
            )

    screenshot_path = target_dir / f"{filename_prefix}tweet.png"
    try:
        _render_tweet_screenshot(snapshot, screenshot_path)
        attempts.append(
            {
                "kind": "screenshot",
                "url": snapshot.url,
                "path": str(screenshot_path),
                "returncode": 0,
                "timed_out": False,
            }
        )
    except Exception as exc:
        attempts.append(
            {
                "kind": "screenshot",
                "url": snapshot.url,
                "returncode": 1,
                "timed_out": False,
                "error": str(exc),
            }
        )
    return attempts


async def _save_reddit_snapshot_asset(
    snapshot: RedditSnapshot,
    target_dir: Path,
    *,
    filename_prefix: str = "",
) -> list[dict[str, Any]]:
    screenshot_path = target_dir / f"{filename_prefix}reddit-post.png"
    try:
        _render_reddit_screenshot(snapshot, screenshot_path)
        return [
            {
                "kind": "screenshot",
                "url": snapshot.url,
                "path": str(screenshot_path),
                "returncode": 0,
                "timed_out": False,
            }
        ]
    except Exception as exc:
        return [
            {
                "kind": "screenshot",
                "url": snapshot.url,
                "returncode": 1,
                "timed_out": False,
                "error": str(exc),
            }
        ]


def _ytdlp_download_command(url: str, target_dir: Path) -> list[str]:
    parsed = urlparse(url)
    is_instagram_post = (
        (parsed.hostname or "").lower() in INSTAGRAM_HOSTS
        and parsed.path.lower().startswith("/p/")
    )
    command = [
        "yt-dlp",
        "--no-playlist",
    ]
    if is_instagram_post:
        # Photo-only posts have no video format, but yt-dlp exposes the original
        # post image as its highest-resolution thumbnail.
        command.extend(["--ignore-no-formats-error", "--write-thumbnail"])
    command.extend(
        [
            "--restrict-filenames",
            "--trim-filenames",
            "180",
            "--merge-output-format",
            "mp4",
            "--js-runtimes",
            "deno",
            "--remote-components",
            "ejs:github",
            "-P",
            str(target_dir),
            "-o",
            "%(title).120B [%(extractor_key)s-%(id)s].%(ext)s",
            url,
        ]
    )
    return command


async def _run_ytdlp(url: str, target_dir: Path, timeout_seconds: int = DOWNLOAD_TIMEOUT_SECONDS) -> DownloadProcessResult:
    command = _ytdlp_download_command(url, target_dir)
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=500, detail="yt-dlp is not installed in the app container.") from exc
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout_seconds)
    except TimeoutError:
        process.kill()
        stdout, stderr = await process.communicate()
        return DownloadProcessResult(
            returncode=124,
            stdout=stdout.decode(errors="replace"),
            stderr=stderr.decode(errors="replace"),
            timed_out=True,
        )
    return DownloadProcessResult(
        returncode=process.returncode or 0,
        stdout=stdout.decode(errors="replace"),
        stderr=stderr.decode(errors="replace"),
    )


async def download_item_media(
    db: Session,
    item_id: int,
    download_root: Path = DOWNLOAD_ROOT,
    timeout_seconds: int = DOWNLOAD_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    item = db.get(Item, item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Clip not found")
    target_dir = _download_dir_for_item(item, download_root)
    target_dir.mkdir(parents=True, exist_ok=True)

    if item.source and item.source.name == "reddit":
        snapshot = _reddit_snapshot_from_item(item)
        attempts: list[dict[str, Any]] = []
        stdout_parts: list[str] = []
        stderr_parts: list[str] = []
        item_host = (urlparse(item.url).hostname or "").lower()
        has_external_media = item_host not in REDDIT_HOSTS
        if item.is_video or has_external_media:
            for url in _download_urls(item):
                result = await _run_ytdlp(url, target_dir, timeout_seconds)
                attempts.append(
                    {
                        "kind": "video" if item.is_video else "media",
                        "url": url,
                        "returncode": result.returncode,
                        "timed_out": result.timed_out,
                    }
                )
                if result.stdout:
                    stdout_parts.append(f"[{url}]\n{result.stdout}")
                if result.stderr:
                    stderr_parts.append(f"[{url}]\n{result.stderr}")
        if snapshot.title.strip() or snapshot.body.strip():
            attempts.extend(await _save_reddit_snapshot_asset(snapshot, target_dir))

        succeeded = sum(attempt["returncode"] == 0 for attempt in attempts)
        status = "success" if succeeded == len(attempts) else "partial" if succeeded else "failed"
        error_parts = [attempt["error"] for attempt in attempts if attempt.get("error")]
        if error_parts:
            stderr_parts.extend(error_parts)
        return {
            "status": status,
            "item_id": item.id,
            "source": "reddit",
            "url": item.url,
            "urls": _download_urls(item),
            "media_count": len(attempts),
            "media_succeeded": succeeded,
            "media_failed": len(attempts) - succeeded,
            "downloads": attempts,
            "download_dir": str(target_dir),
            "host_dir": str(Path("./data/downloads") / target_dir.relative_to(download_root)),
            "files": _downloaded_files(target_dir, download_root),
            "returncode": 0 if status == "success" else 1,
            "timed_out": any(attempt["timed_out"] for attempt in attempts),
            "stdout_tail": _tail("\n".join(stdout_parts)),
            "stderr_tail": _tail("\n".join(stderr_parts)),
        }

    if item.source and item.source.name == "x":
        snapshot = _x_snapshot_from_item(item)
        attempts: list[dict[str, Any]] = []
        stdout_parts: list[str] = []
        stderr_parts: list[str] = []
        if item.is_video:
            result = await _run_ytdlp(item.url, target_dir, timeout_seconds)
            attempts.append(
                {
                    "kind": "video",
                    "url": item.url,
                    "returncode": result.returncode,
                    "timed_out": result.timed_out,
                }
            )
            if result.stdout:
                stdout_parts.append(f"[{item.url}]\n{result.stdout}")
            if result.stderr:
                stderr_parts.append(f"[{item.url}]\n{result.stderr}")
            if result.returncode != 0:
                attempts.extend(await _save_x_snapshot_assets(snapshot, target_dir))
        else:
            attempts.extend(await _save_x_snapshot_assets(snapshot, target_dir))

        succeeded = sum(attempt["returncode"] == 0 for attempt in attempts)
        status = "success" if succeeded == len(attempts) else "partial" if succeeded else "failed"
        error_parts = [attempt["error"] for attempt in attempts if attempt.get("error")]
        if error_parts:
            stderr_parts.extend(error_parts)
        return {
            "status": status,
            "item_id": item.id,
            "source": "x",
            "url": item.url,
            "urls": [item.url, *(snapshot.image_urls or [])],
            "media_count": len(attempts),
            "media_succeeded": succeeded,
            "media_failed": len(attempts) - succeeded,
            "downloads": attempts,
            "download_dir": str(target_dir),
            "host_dir": str(Path("./data/downloads") / target_dir.relative_to(download_root)),
            "files": _downloaded_files(target_dir, download_root),
            "returncode": 0 if status == "success" else 1,
            "timed_out": any(attempt["timed_out"] for attempt in attempts),
            "stdout_tail": _tail("\n".join(stdout_parts)),
            "stderr_tail": _tail("\n".join(stderr_parts)),
        }

    urls = _download_urls(item)
    if not urls:
        raise HTTPException(status_code=400, detail="Clip has no downloadable media URL")
    attempts: list[dict[str, Any]] = []
    stdout_parts: list[str] = []
    stderr_parts: list[str] = []
    for url in urls:
        result = await _run_ytdlp(url, target_dir, timeout_seconds)
        attempts.append(
            {
                "url": url,
                "returncode": result.returncode,
                "timed_out": result.timed_out,
            }
        )
        if result.stdout:
            stdout_parts.append(f"[{url}]\n{result.stdout}")
        if result.stderr:
            stderr_parts.append(f"[{url}]\n{result.stderr}")
    succeeded = sum(attempt["returncode"] == 0 for attempt in attempts)
    status = "success" if succeeded == len(attempts) else "partial" if succeeded else "failed"
    return {
        "status": status,
        "item_id": item.id,
        "source": item.source.name if item.source else "unknown",
        "url": item.url,
        "urls": urls,
        "media_count": len(urls),
        "media_succeeded": succeeded,
        "media_failed": len(urls) - succeeded,
        "downloads": attempts,
        "download_dir": str(target_dir),
        "host_dir": str(Path("./data/downloads") / target_dir.relative_to(download_root)),
        "files": _downloaded_files(target_dir, download_root),
        "returncode": 0 if status == "success" else 1,
        "timed_out": any(attempt["timed_out"] for attempt in attempts),
        "stdout_tail": _tail("\n".join(stdout_parts)),
        "stderr_tail": _tail("\n".join(stderr_parts)),
    }


async def download_links(
    urls: list[str],
    download_root: Path = DOWNLOAD_ROOT,
    timeout_seconds: int = DOWNLOAD_TIMEOUT_SECONDS,
    concurrency: int = LINK_DOWNLOAD_CONCURRENCY,
) -> dict[str, Any]:
    normalized: list[tuple[str, str, str]] = []
    invalid: list[dict[str, Any]] = []
    seen: set[str] = set()
    duplicates_skipped = 0
    for value in urls:
        try:
            normalized_url, source, link_key = _normalize_download_url(value)
        except ValueError as exc:
            invalid.append(
                {
                    "input_url": value,
                    "url": None,
                    "status": "invalid",
                    "returncode": None,
                    "timed_out": False,
                    "error": str(exc),
                    "files": [],
                }
            )
            continue
        deduplication_key = f"{source}:{link_key}"
        if deduplication_key in seen:
            duplicates_skipped += 1
            continue
        seen.add(deduplication_key)
        normalized.append((normalized_url, source, link_key))

    semaphore = asyncio.Semaphore(max(1, min(concurrency, 5)))

    async def download_one(entry: tuple[str, str, str]) -> dict[str, Any]:
        normalized_url, source, link_key = entry
        target_dir = _download_dir_for_link(download_root)
        target_dir.mkdir(parents=True, exist_ok=True)
        work_dir = _link_work_dir(source, link_key, download_root)
        try:
            async with semaphore:
                try:
                    result = await _run_ytdlp(normalized_url, work_dir, timeout_seconds)
                except Exception as exc:
                    if source not in {"x", "reddit"}:
                        raise
                    result = DownloadProcessResult(returncode=1, stdout="", stderr=str(exc))

                completed_by_ytdlp = any(
                    path.is_file() and path.suffix.lower() not in {".part", ".ytdl"}
                    for path in work_dir.rglob("*")
                )
                fallback_attempts: list[dict[str, Any]] = []
                fallback_error = ""
                if source == "x" and (result.returncode != 0 or not completed_by_ytdlp):
                    try:
                        snapshot = await _fetch_x_snapshot(normalized_url)
                        prefix = f"x-{_safe_segment(snapshot.handle, 'user', lowercase=True)}-{snapshot.status_id}-"
                        fallback_attempts = await _save_x_snapshot_assets(
                            snapshot,
                            work_dir,
                            filename_prefix=prefix,
                        )
                    except Exception as exc:
                        fallback_error = str(exc)
                elif source == "reddit":
                    try:
                        snapshot = await _fetch_reddit_snapshot(normalized_url)
                        should_save_screenshot = bool(snapshot.body.strip()) or not completed_by_ytdlp
                        if should_save_screenshot:
                            prefix = f"reddit-{_safe_segment(snapshot.subreddit, 'reddit', lowercase=True)}-{snapshot.post_id}-"
                            fallback_attempts = await _save_reddit_snapshot_asset(
                                snapshot,
                                work_dir,
                                filename_prefix=prefix,
                            )
                    except Exception as exc:
                        fallback_error = str(exc)
        except Exception:
            shutil.rmtree(work_dir, ignore_errors=True)
            raise
        files = _move_completed_files(work_dir, target_dir, download_root)
        fallback_succeeded = any(attempt["returncode"] == 0 for attempt in fallback_attempts)
        instagram_image_succeeded = source == "instagram" and any(
            Path(file["name"]).suffix.lower() in {".gif", ".jpeg", ".jpg", ".png", ".webp"}
            for file in files
        )
        succeeded = bool(files) and (
            result.returncode == 0 or fallback_succeeded or instagram_image_succeeded
        )
        fallback_kind = None
        if fallback_succeeded:
            fallback_kind = f"{source}_screenshot"
        elif instagram_image_succeeded and result.returncode != 0:
            fallback_kind = "instagram_image"
        stderr = result.stderr
        if fallback_error:
            stderr = f"{stderr}\n{source.title()} screenshot: {fallback_error}".strip()
        failed_fallbacks = [attempt["error"] for attempt in fallback_attempts if attempt.get("error")]
        if failed_fallbacks:
            stderr = f"{stderr}\n" + "\n".join(failed_fallbacks)
        return {
            "input_url": normalized_url,
            "url": normalized_url,
            "source": source,
            "status": "success" if succeeded else "failed",
            "returncode": 0 if succeeded else result.returncode,
            "timed_out": result.timed_out,
            "fallback": fallback_kind,
            "fallback_attempts": fallback_attempts,
            "download_dir": str(target_dir),
            "host_dir": str(Path("./data/downloads") / target_dir.relative_to(download_root)),
            "files": files,
            "stdout_tail": _tail(result.stdout),
            "stderr_tail": _tail(stderr),
        }

    downloads = list(await asyncio.gather(*(download_one(entry) for entry in normalized)))
    succeeded = sum(entry["status"] == "success" for entry in downloads)
    failed_downloads = len(downloads) - succeeded
    failed = failed_downloads + len(invalid)
    status = "success" if succeeded and failed == 0 else "partial" if succeeded else "failed"
    batch_dir = download_root / "link-downloader"
    source_counts = {
        source: sum(entry_source == source for _url, entry_source, _key in normalized)
        for source in ("x", "youtube", "reddit", "instagram", "twitch", "kick", "rumble")
        if any(entry_source == source for _url, entry_source, _key in normalized)
    }
    return {
        "status": status,
        "requested_count": len(urls),
        "unique_count": len(normalized),
        "duplicates_skipped": duplicates_skipped,
        "invalid_count": len(invalid),
        "succeeded": succeeded,
        "failed": failed,
        "source_counts": source_counts,
        "download_dir": str(batch_dir),
        "host_dir": str(Path("./data/downloads/link-downloader")),
        "downloads": [*downloads, *invalid],
        "files": _downloaded_files(batch_dir, download_root),
    }


async def download_x_links(
    urls: list[str],
    download_root: Path = DOWNLOAD_ROOT,
    timeout_seconds: int = DOWNLOAD_TIMEOUT_SECONDS,
    concurrency: int = LINK_DOWNLOAD_CONCURRENCY,
) -> dict[str, Any]:
    return await download_links(urls, download_root, timeout_seconds, concurrency)


async def research_pinterest_images(
    query: str,
    limit: int = 8,
    download_root: Path = DOWNLOAD_ROOT,
    concurrency: int = 4,
) -> dict[str, Any]:
    search_url, pins = await search_public_pinterest(query, limit)
    target_dir = download_root / "pinterest" / pinterest_query_slug(query)
    target_dir.mkdir(parents=True, exist_ok=True)
    semaphore = asyncio.Semaphore(max(1, min(concurrency, 5)))

    async def download_one(pin) -> dict[str, Any]:
        title = pin.title or pin.description or "pinterest-image"
        stem = target_dir / f"{_safe_segment(title, 'pinterest-image', max_length=90)} [Pinterest-{pin.pin_id}]"
        entry = pin.as_dict()
        try:
            async with semaphore:
                path = await download_pinterest_pin(pin, stem)
            entry.update({"status": "success", "file": _file_info(path, download_root), "error": None})
        except Exception as exc:
            entry.update({"status": "failed", "file": None, "error": str(exc)})
        return entry

    downloads = list(await asyncio.gather(*(download_one(pin) for pin in pins)))
    succeeded = sum(entry["status"] == "success" for entry in downloads)
    failed = len(downloads) - succeeded
    status = "success" if succeeded and not failed else "partial" if succeeded else "failed"
    files = [entry["file"] for entry in downloads if entry.get("file")]
    return {
        "status": status,
        "query": " ".join(query.split()).strip(),
        "search_url": search_url,
        "requested_count": limit,
        "pins_found": len(pins),
        "succeeded": succeeded,
        "failed": failed,
        "download_dir": str(target_dir),
        "host_dir": str(Path("./data/downloads") / target_dir.relative_to(download_root)),
        "downloads": downloads,
        "files": files,
        "rights_note": "Pinterest results may be copyrighted. Verify permission and usage rights before republishing them.",
    }


@router.get("/downloads/files/{file_path:path}")
async def download_saved_file(file_path: str, inline: bool = False):
    path = _resolve_download_file(file_path)
    return FileResponse(path, filename=None if inline else path.name)


@router.post("/downloads/items/{item_id}")
async def download_item_endpoint(item_id: int, db: Session = Depends(get_db)):
    return await download_item_media(db, item_id)


@router.post("/downloads/x-links")
async def download_x_links_endpoint(request: XLinksDownloadRequest):
    return await download_links(request.urls)


@router.post("/downloads/links")
async def download_links_endpoint(request: LinksDownloadRequest):
    return await download_links(request.urls)


@router.post("/research/pinterest-images")
async def research_pinterest_images_endpoint(request: PinterestImageResearchRequest):
    try:
        return await research_pinterest_images(request.query, request.limit)
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(status_code=502, detail=f"Pinterest image research failed: {exc}") from exc


@router.post("/research/pinterest-search")
async def pinterest_search_only_endpoint(request: PinterestImageResearchRequest):
    """Search public Pinterest pins and return metadata without downloading images."""
    try:
        search_url, pins = await search_public_pinterest(request.query, request.limit)
        return {
            "status": "success",
            "query": " ".join(request.query.split()).strip(),
            "search_url": search_url,
            "pins_found": len(pins),
            "pins": [pin.as_dict() for pin in pins],
            "rights_note": "Pinterest results may be copyrighted. Verify permission and usage rights before republishing them.",
        }
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(status_code=502, detail=f"Pinterest search failed: {exc}") from exc


@router.post("/research/pinterest-download")
async def pinterest_download_pins_endpoint(request: dict):
    """Download images for previously searched Pinterest pins."""
    pins_data = request.get("pins", [])
    query = request.get("query", "pinterest-images")
    if not pins_data:
        raise HTTPException(status_code=400, detail="No pins to download")
    target_dir = DOWNLOAD_ROOT / "pinterest" / pinterest_query_slug(query)
    target_dir.mkdir(parents=True, exist_ok=True)
    semaphore = asyncio.Semaphore(4)

    from app.pinterest_client import PinterestPin

    async def download_one(pin_dict: dict) -> dict[str, Any]:
        pin = PinterestPin(
            pin_id=str(pin_dict.get("pin_id", "")),
            title=pin_dict.get("title", ""),
            description=pin_dict.get("description", ""),
            pin_url=pin_dict.get("pin_url", ""),
            image_url=pin_dict.get("image_url", ""),
            width=pin_dict.get("width"),
            height=pin_dict.get("height"),
            pinner=pin_dict.get("pinner"),
        )
        title = pin.title or pin.description or "pinterest-image"
        stem = target_dir / f"{_safe_segment(title, 'pinterest-image', max_length=90)} [Pinterest-{pin.pin_id}]"
        entry = pin.as_dict()
        try:
            async with semaphore:
                path = await download_pinterest_pin(pin, stem)
            entry.update({"status": "success", "file": _file_info(path, DOWNLOAD_ROOT), "error": None})
        except Exception as exc:
            entry.update({"status": "failed", "file": None, "error": str(exc)})
        return entry

    downloads = list(await asyncio.gather(*(download_one(p) for p in pins_data)))
    succeeded = sum(entry["status"] == "success" for entry in downloads)
    failed = len(downloads) - succeeded
    status = "success" if succeeded and not failed else "partial" if succeeded else "failed"
    files = [entry["file"] for entry in downloads if entry.get("file")]
    return {
        "status": status,
        "query": " ".join(query.split()).strip(),
        "requested_count": len(pins_data),
        "pins_found": len(pins_data),
        "succeeded": succeeded,
        "failed": failed,
        "download_dir": str(target_dir),
        "host_dir": str(Path("./data/downloads") / target_dir.relative_to(DOWNLOAD_ROOT)),
        "downloads": downloads,
        "files": files,
        "rights_note": "Pinterest results may be copyrighted. Verify permission and usage rights before republishing them.",
    }
