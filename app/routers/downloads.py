import asyncio
import html
import json
import mimetypes
import re
import shutil
import tempfile
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlparse

import httpx
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Item
from app.schemas import LinksDownloadRequest, XLinksDownloadRequest
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
        snapshot.url,
        font=footer_font,
        fill="#536471",
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
    raise ValueError("Only X/Twitter, YouTube, and Reddit video/post links are supported")


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


async def _run_ytdlp(url: str, target_dir: Path, timeout_seconds: int = DOWNLOAD_TIMEOUT_SECONDS) -> DownloadProcessResult:
    command = [
        "yt-dlp",
        "--no-playlist",
        "--restrict-filenames",
        "--trim-filenames",
        "180",
        "--merge-output-format",
        "mp4",
        "--js-runtimes",
        "deno",
        "-P",
        str(target_dir),
        "-o",
        "%(title).120B [%(extractor_key)s-%(id)s].%(ext)s",
        url,
    ]
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
                    if source != "x":
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
        except Exception:
            shutil.rmtree(work_dir, ignore_errors=True)
            raise
        files = _move_completed_files(work_dir, target_dir, download_root)
        fallback_succeeded = any(attempt["returncode"] == 0 for attempt in fallback_attempts)
        succeeded = bool(files) and (result.returncode == 0 or fallback_succeeded)
        stderr = result.stderr
        if fallback_error:
            stderr = f"{stderr}\nScreenshot fallback: {fallback_error}".strip()
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
            "fallback": "x_screenshot" if fallback_succeeded else None,
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
        for source in ("x", "youtube", "reddit")
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


@router.get("/downloads/files/{file_path:path}")
async def download_saved_file(file_path: str):
    path = _resolve_download_file(file_path)
    return FileResponse(path, filename=path.name)


@router.post("/downloads/items/{item_id}")
async def download_item_endpoint(item_id: int, db: Session = Depends(get_db)):
    return await download_item_media(db, item_id)


@router.post("/downloads/x-links")
async def download_x_links_endpoint(request: XLinksDownloadRequest):
    return await download_links(request.urls)


@router.post("/downloads/links")
async def download_links_endpoint(request: LinksDownloadRequest):
    return await download_links(request.urls)
