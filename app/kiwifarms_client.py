import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from html.parser import HTMLParser
from typing import Any, Callable, Iterator
from urllib.parse import unquote, urlencode, urljoin, urlparse, urlunparse

import httpx
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.models import Item, Media, Run, Source, utcnow
from app.ranking import rank_item
from app.schemas import KiwiFarmsCollectRequest
from app.web_headers import DEFAULT_HEADERS, web_client_kwargs

TEMPORARY_STATUS_CODES = {500, 502, 503, 504}
BRIDGE_SEARCH_LIMIT = 20
BRIDGE_THREAD_POST_LIMIT = 50
ARCHIVE_MEDIA_HOSTS = frozenset(
    {"archive.ph", "archive.vn", "archive.is", "archive.today", "archive.md", "archive.fo", "archive.li"}
)
VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
THREAD_ID_PATTERNS = (
    re.compile(r"/threads/(?:[^/?#]*\.)?(\d+)(?:/|$)", re.IGNORECASE),
    re.compile(r"[?&]thread_id=(\d+)", re.IGNORECASE),
)
POST_ID_PATTERNS = (
    re.compile(r"(?:/|#)post-(\d+)(?:/|$)", re.IGNORECASE),
    re.compile(r"/posts/(\d+)(?:/|$)", re.IGNORECASE),
    re.compile(r"[?&]post_id=(\d+)", re.IGNORECASE),
)
CHALLENGE_MARKERS = (
    "data-ttrs-challenge",
    "ttrs-page--challenge",
    "cf-chl-",
    "cloudflare ray id",
    "checking your browser",
    "just a moment...",
    "please complete the security check",
    "captcha",
)
NO_RESULTS_MARKERS = (
    "no results found",
    "your search returned no results",
    "could not find any results",
    "no search results",
)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, default=str)


@dataclass
class _Node:
    tag: str
    attrs: dict[str, str]
    parent: "_Node | None" = None
    children: list["_Node"] = field(default_factory=list)
    text_parts: list[str] = field(default_factory=list)

    @property
    def classes(self) -> set[str]:
        return set(self.attrs.get("class", "").split())

    def text(self) -> str:
        parts = [*self.text_parts]
        for child in self.children:
            parts.append(child.text())
        return re.sub(r"\s+", " ", unescape(" ".join(parts))).strip()


class _TreeParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node("document", {})
        self.stack = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = _Node(tag.lower(), {key.lower(): value or "" for key, value in attrs}, self.stack[-1])
        self.stack[-1].children.append(node)
        if tag.lower() not in VOID_TAGS:
            self.stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag.lower() not in VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                return

    def handle_data(self, data: str) -> None:
        if data.strip():
            self.stack[-1].text_parts.append(data)


def _parse_tree(html_text: str) -> _Node:
    parser = _TreeParser()
    parser.feed(html_text)
    return parser.root


def _walk(node: _Node) -> Iterator[_Node]:
    yield node
    for child in node.children:
        yield from _walk(child)


def _class_contains(node: _Node, *fragments: str) -> bool:
    lowered = [class_name.lower() for class_name in node.classes]
    return any(fragment.lower() in class_name for fragment in fragments for class_name in lowered)


def _first_node(node: _Node, predicate: Callable[[_Node], bool]) -> _Node | None:
    return next((candidate for candidate in _walk(node) if predicate(candidate)), None)


def _absolute_url(value: str | None, base_url: str) -> str | None:
    if not value:
        return None
    value = unescape(value).strip()
    if value.startswith(("javascript:", "data:", "mailto:")):
        return None
    absolute = urljoin(base_url.rstrip("/") + "/", value)
    parsed = urlparse(absolute)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return urlunparse(parsed._replace(fragment=parsed.fragment))


def _id_from_patterns(value: str | None, patterns: tuple[re.Pattern[str], ...]) -> str | None:
    if not value:
        return None
    for pattern in patterns:
        match = pattern.search(value)
        if match:
            return match.group(1)
    return None


def _thread_url(value: str | None, base_url: str) -> str | None:
    absolute = _absolute_url(value, base_url)
    if not absolute or not _id_from_patterns(absolute, THREAD_ID_PATTERNS):
        return None
    parsed = urlparse(absolute)
    path = re.sub(r"/post-\d+/?$", "/", parsed.path, flags=re.IGNORECASE)
    return urlunparse(parsed._replace(path=path, query="", fragment=""))


def _parse_number(value: str | None) -> int | None:
    if not value:
        return None
    match = re.search(r"([\d,.]+)\s*([kKmM]?)", value)
    if not match:
        return None
    try:
        number = float(match.group(1).replace(",", ""))
    except ValueError:
        return None
    multiplier = 1_000 if match.group(2).lower() == "k" else 1_000_000 if match.group(2).lower() == "m" else 1
    return int(number * multiplier)


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        try:
            return datetime.fromtimestamp(int(value), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def redact_personal_information(value: str | None) -> str:
    """Remove common doxxing signals from snippets before they are stored or shown."""
    if not value:
        return ""
    text = unescape(value)
    text = re.sub(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", "[REDACTED EMAIL]", text, flags=re.IGNORECASE)
    text = re.sub(
        r"(?<!\w)(?:\+?1[ .-]?)?(?:\(\d{3}\)|\d{3})[ .-]\d{3}[ .-]\d{4}(?!\w)",
        "[REDACTED PHONE]",
        text,
    )
    text = re.sub(
        r"\b\d{1,6}\s+(?:[NSEW]\.?(?:orth|outh|ast|est)?\s+)?(?:[A-Z][\w'.-]*\s+){0,4}"
        r"(?:Street|St\.?|Avenue|Ave\.?|Road|Rd\.?|Boulevard|Blvd\.?|Lane|Ln\.?|Drive|Dr\.?|Court|Ct\.?|Way)\b"
        r"(?:\s*,?\s*(?:Apt\.?|Apartment|Unit|Suite|#)\s*[A-Z0-9-]+)?",
        "[REDACTED ADDRESS]",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"\b(?:mother|father|sister|brother|wife|husband|daughter|son|child)\s*(?:is|:|-)?\s+"
        r"[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?\b",
        "[REDACTED FAMILY INFORMATION]",
        text,
    )
    text = re.sub(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", "[REDACTED IP ADDRESS]", text)
    return re.sub(r"\s+", " ", text).strip()


def _media_type(url: str) -> str | None:
    parsed = urlparse(url)
    host = parsed.netloc.lower().removeprefix("www.")
    path = parsed.path.lower()
    if re.search(r"\.(?:mp4|webm|mov|m4v|m3u8)(?:$|[?#])", url, re.IGNORECASE):
        return "direct_video"
    if host in {"x.com", "twitter.com", "mobile.twitter.com"} and re.search(r"/status(?:es)?/\d+", path):
        return "x_status"
    if host in {"youtube.com", "m.youtube.com"} and (path == "/watch" or path.startswith("/shorts/")):
        return "youtube"
    if host == "youtu.be" and path.strip("/"):
        return "youtube"
    if host == "clips.twitch.tv" or (host.endswith("twitch.tv") and "/clip/" in path):
        return "twitch_clip"
    if host.endswith("reddit.com") and ("/comments/" in path or path.endswith("/video")):
        return "reddit"
    if host == "v.redd.it":
        return "reddit_video"
    if host.endswith("streamable.com") and path.strip("/"):
        return "streamable"
    if host.endswith("tiktok.com") and "/video/" in path:
        return "tiktok"
    return None


def _normalized_media_url(value: str) -> tuple[str, str] | None:
    """Return a downloadable media URL, unwrapping archive redirect links when present."""

    variants = [unescape(value.strip())]
    for _ in range(3):
        decoded = unquote(variants[-1])
        if decoded == variants[-1]:
            break
        variants.append(decoded)

    for variant in reversed(variants):
        starts = [match.start() for match in re.finditer(r"https?://", variant, re.IGNORECASE)]
        for start in reversed(starts):
            candidate = variant[start:]
            parsed = urlparse(candidate)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                continue
            media_type = _media_type(candidate)
            if media_type:
                return urlunparse(parsed._replace(fragment="")), media_type
            if parsed.netloc.lower().removeprefix("www.") not in ARCHIVE_MEDIA_HOSTS:
                break
    return None


def extract_media_links(node: _Node, base_url: str) -> list[dict[str, Any]]:
    candidates: list[str] = []
    for candidate in _walk(node):
        if candidate.tag == "a":
            absolute = _absolute_url(candidate.attrs.get("href"), base_url)
            if absolute:
                candidates.append(absolute)
        for match in URL_RE.findall(" ".join(candidate.text_parts)):
            candidates.append(unescape(match).rstrip(".,);]"))
    media: list[dict[str, Any]] = []
    seen: set[str] = set()
    for url in candidates:
        normalized_media = _normalized_media_url(url)
        if not normalized_media:
            continue
        normalized, media_type = normalized_media
        if normalized in seen:
            continue
        seen.add(normalized)
        media.append({"url": normalized, "type": media_type})
    return media


def _best_media_url(media: list[dict[str, Any]]) -> str | None:
    priority = {
        "direct_video": 0,
        "youtube": 1,
        "twitch_clip": 2,
        "streamable": 3,
        "x_status": 4,
        "tiktok": 5,
        "reddit_video": 6,
        "reddit": 7,
    }
    if not media:
        return None
    return min(media, key=lambda entry: priority.get(entry["type"], 99))["url"]


def _counter(container: _Node, label: str) -> int | None:
    for candidate in _walk(container):
        if candidate.tag == "dt" and candidate.text().strip().lower().rstrip(":") == label:
            parent = candidate.parent
            if parent:
                siblings = parent.children
                try:
                    index = siblings.index(candidate)
                except ValueError:
                    continue
                for sibling in siblings[index + 1 :]:
                    if sibling.tag == "dd":
                        return _parse_number(sibling.text())
    match = re.search(rf"\b{re.escape(label)}\s*:?\s*([\d,.]+\s*[kKmM]?)", container.text(), flags=re.IGNORECASE)
    return _parse_number(match.group(1)) if match else None


def _result_containers(root: _Node) -> list[_Node]:
    containers: list[_Node] = []
    for node in _walk(root):
        if node.tag not in {"li", "article", "div"}:
            continue
        looks_like_result = _class_contains(node, "block-row--separated", "searchresult", "structitem")
        if not looks_like_result:
            continue
        if any(_id_from_patterns(link.attrs.get("href"), THREAD_ID_PATTERNS + POST_ID_PATTERNS) for link in _walk(node) if link.tag == "a"):
            containers.append(node)
    if containers:
        return containers

    seen: set[int] = set()
    for link in _walk(root):
        if link.tag != "a" or not _id_from_patterns(link.attrs.get("href"), THREAD_ID_PATTERNS + POST_ID_PATTERNS):
            continue
        container = link.parent
        while container and container.tag not in {"li", "article"}:
            container = container.parent
        if container and id(container) not in seen:
            seen.add(id(container))
            containers.append(container)
    return containers


def _parse_result(container: _Node, base_url: str) -> dict[str, Any] | None:
    links = [node for node in _walk(container) if node.tag == "a" and node.attrs.get("href")]
    direct_link = next((link for link in links if _id_from_patterns(link.attrs.get("href"), POST_ID_PATTERNS)), None)
    thread_link = next((link for link in links if _id_from_patterns(link.attrs.get("href"), THREAD_ID_PATTERNS)), None)
    permalink = _absolute_url(direct_link.attrs.get("href"), base_url) if direct_link else None
    thread_url = _thread_url(thread_link.attrs.get("href"), base_url) if thread_link else _thread_url(permalink, base_url)
    permalink = permalink or thread_url
    thread_id = _id_from_patterns(thread_url or permalink, THREAD_ID_PATTERNS)
    post_id = _id_from_patterns(permalink, POST_ID_PATTERNS)
    if not permalink or not (thread_id or post_id):
        return None

    title_node = _first_node(container, lambda node: _class_contains(node, "contentrow-title", "structitem-title", "searchresult-title"))
    title = title_node.text() if title_node else thread_link.text() if thread_link else direct_link.text() if direct_link else ""
    excerpt_node = _first_node(container, lambda node: _class_contains(node, "contentrow-snippet", "searchresult-snippet", "message-body"))
    excerpt = excerpt_node.text() if excerpt_node else ""
    author = container.attrs.get("data-author") or None
    if not author:
        author_node = _first_node(
            container,
            lambda node: node.tag == "a" and ("/members/" in node.attrs.get("href", "") or _class_contains(node, "username")),
        )
        author = author_node.text() if author_node else None
    author = redact_personal_information(author) or None
    time_node = _first_node(container, lambda node: node.tag == "time" or _class_contains(node, "u-dt"))
    date_value = None
    if time_node:
        date_value = time_node.attrs.get("datetime") or time_node.attrs.get("data-time") or time_node.attrs.get("title")
    forum_node = _first_node(container, lambda node: node.tag == "a" and "/forums/" in node.attrs.get("href", ""))
    relevance_node = _first_node(container, lambda node: _class_contains(node, "searchresult-score", "relevance"))
    relevance = container.attrs.get("data-score") or (relevance_node.text() if relevance_node else None)
    media = extract_media_links(container, base_url)
    external_id = f"thread:{thread_id}:post:{post_id}" if thread_id and post_id else f"thread:{thread_id}" if thread_id else f"post:{post_id}"
    return {
        "external_id": external_id,
        "thread_id": thread_id,
        "post_id": post_id,
        "title": redact_personal_information(title)[:500],
        "excerpt": redact_personal_information(excerpt)[:1200],
        "permalink": permalink,
        "thread_url": thread_url,
        "author": author,
        "created_time": _parse_datetime(date_value),
        "forum": forum_node.text() if forum_node else None,
        "replies": _counter(container, "replies"),
        "views": _counter(container, "views"),
        "relevance": relevance,
        "media": media,
    }


def parse_search_results(html_text: str, base_url: str, limit: int = 100) -> list[dict[str, Any]]:
    root = _parse_tree(html_text)
    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    for container in _result_containers(root):
        result = _parse_result(container, base_url)
        if not result or result["external_id"] in seen:
            continue
        seen.add(result["external_id"])
        results.append(result)
        if len(results) >= limit:
            break
    return results


def _next_page_url(html_text: str, base_url: str) -> str | None:
    root = _parse_tree(html_text)
    link = _first_node(
        root,
        lambda node: node.tag == "a"
        and (
            node.attrs.get("rel", "").lower() == "next"
            or _class_contains(node, "pagenav-jump--next")
            or node.text().strip().lower() in {"next", "next >", "next ›"}
        ),
    )
    return _absolute_url(link.attrs.get("href"), base_url) if link else None


def _extract_search_form(html_text: str, base_url: str) -> tuple[str, str, str, dict[str, str]] | None:
    root = _parse_tree(html_text)
    for form in (node for node in _walk(root) if node.tag == "form"):
        inputs = [node for node in _walk(form) if node.tag == "input" and node.attrs.get("name")]
        keyword_input = next(
            (node for node in inputs if node.attrs.get("name", "").lower() in {"keywords", "q", "query", "search"}),
            None,
        )
        action = _absolute_url(form.attrs.get("action") or "/search/search", base_url)
        if not keyword_input or not action or "search" not in urlparse(action).path.lower():
            continue
        if urlparse(action).netloc.lower() != urlparse(base_url).netloc.lower():
            continue
        fields: dict[str, str] = {}
        for node in inputs:
            name = node.attrs.get("name", "")
            if node.attrs.get("type", "").lower() == "hidden" and (
                name.startswith("_xf")
                or name.startswith("c[")
                or name in {"order", "search_type", "type", "users", "constraints", "date", "before"}
            ):
                fields[name] = node.attrs.get("value", "")
        return action, form.attrs.get("method", "get").lower(), keyword_input.attrs["name"], fields
    return None


def _detect_access_issue(response: httpx.Response) -> str | None:
    text = response.text.lower()
    if response.status_code == 403:
        return "the site returned HTTP 403 Forbidden"
    if response.status_code == 429:
        return "the site returned HTTP 429 Too Many Requests"
    if any(marker in text for marker in CHALLENGE_MARKERS):
        return "the site returned a CAPTCHA or JavaScript security challenge"
    if "must be logged in" in text or "login required" in text or "you must log in" in text:
        return "public search currently requires authentication"
    if response.status_code >= 400:
        return f"the site returned HTTP {response.status_code}"
    return None


def _is_no_results(html_text: str) -> bool:
    lowered = html_text.lower()
    return any(marker in lowered for marker in NO_RESULTS_MARKERS)


def _enrich_from_post_html(result: dict[str, Any], html_text: str, base_url: str) -> None:
    root = _parse_tree(html_text)
    post_id = result.get("post_id")
    target = None
    if post_id:
        target = _first_node(
            root,
            lambda node: node.attrs.get("id") in {f"post-{post_id}", f"js-post-{post_id}"}
            or node.attrs.get("data-content") in {f"post-{post_id}", f"js-post-{post_id}"},
        )
    target = target or _first_node(root, lambda node: node.tag in {"article", "div"} and _class_contains(node, "message--post"))
    if not target:
        return
    detail_media = extract_media_links(target, base_url)
    seen = {entry["url"] for entry in result["media"]}
    result["media"].extend(entry for entry in detail_media if entry["url"] not in seen)
    if not result.get("excerpt"):
        body = _first_node(target, lambda node: _class_contains(node, "message-body", "bbwrapper"))
        if body:
            result["excerpt"] = redact_personal_information(body.text())[:1200]


class PublicSearchUnavailable(RuntimeError):
    def __init__(self, reason: str, *, stop_fallbacks: bool = False) -> None:
        super().__init__(reason)
        self.stop_fallbacks = stop_fallbacks


@dataclass
class _RequestState:
    delay_seconds: float
    seen: set[str] = field(default_factory=set)
    last_request_at: float | None = None

    async def request(self, client: httpx.AsyncClient, method: str, url: str, **kwargs: Any) -> httpx.Response:
        signature_data = kwargs.get("params") or kwargs.get("data") or {}
        signature = f"{method.upper()} {url} {urlencode(sorted(signature_data.items()))}"
        if signature in self.seen:
            raise PublicSearchUnavailable("the same public page was requested twice due to an unexpected pagination loop")
        self.seen.add(signature)
        if self.last_request_at is not None and self.delay_seconds > 0:
            elapsed = time.monotonic() - self.last_request_at
            if elapsed < self.delay_seconds:
                await asyncio.sleep(self.delay_seconds - elapsed)
        response: httpx.Response | None = None
        for attempt in range(2):
            response = await client.request(method, url, **kwargs)
            self.last_request_at = time.monotonic()
            if response.status_code not in TEMPORARY_STATUS_CODES or attempt == 1:
                return response
            if self.delay_seconds > 0:
                await asyncio.sleep(self.delay_seconds)
        assert response is not None
        return response


def _bridge_endpoint(bridge_url: str, path: str) -> str:
    value = bridge_url.strip().rstrip("/")
    parsed = urlparse(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise PublicSearchUnavailable("the configured bridge URL is invalid")
    return f"{value}/{path.lstrip('/')}"


def _bridge_error_reason(response: httpx.Response) -> str:
    status = f"HTTP {response.status_code}"
    try:
        payload = response.json()
    except ValueError:
        return f"the bridge returned {status} outside its JSON API"
    if isinstance(payload, dict) and isinstance(payload.get("detail"), dict):
        detail = payload["detail"]
        code = str(detail.get("error") or "bridge_error")
        message = str(detail.get("message") or "The bridge could not complete the request.")
        return f"the bridge reported {code}: {message}"
    return f"the bridge returned {status} with an unrecognized error payload"


async def _bridge_json(
    client: httpx.AsyncClient,
    state: _RequestState,
    bridge_url: str,
    path: str,
    params: dict[str, Any],
) -> dict[str, Any]:
    response = await state.request(client, "GET", _bridge_endpoint(bridge_url, path), params=params)
    if response.status_code != 200:
        raise PublicSearchUnavailable(_bridge_error_reason(response), stop_fallbacks=True)
    try:
        payload = response.json()
    except ValueError as exc:
        raise PublicSearchUnavailable("the bridge returned a non-JSON success response", stop_fallbacks=True) from exc
    if not isinstance(payload, dict):
        raise PublicSearchUnavailable("the bridge returned an unrecognized JSON response", stop_fallbacks=True)
    return payload


def _verified_bridge_thread_url(value: Any, thread_id: str) -> str | None:
    if not isinstance(value, str):
        return None
    parsed = urlparse(value)
    try:
        port = parsed.port
    except ValueError:
        return None
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password or port not in {None, 443}:
        return None
    normalized = _thread_url(value, value)
    if not normalized or _id_from_patterns(normalized, THREAD_ID_PATTERNS) != thread_id:
        return None
    return normalized


def _bridge_search_result(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    thread_id = str(value.get("thread_id") or "")
    title = value.get("title")
    if not thread_id.isdecimal() or not isinstance(title, str) or not title.strip():
        return None
    thread_url = _verified_bridge_thread_url(value.get("url"), thread_id)
    if not thread_url:
        return None
    author = value.get("author_display_name") if isinstance(value.get("author_display_name"), str) else None
    created_at = value.get("created_at") if isinstance(value.get("created_at"), str) else None
    last_activity_at = value.get("last_activity_at") if isinstance(value.get("last_activity_at"), str) else None
    return {
        "external_id": f"thread:{thread_id}",
        "thread_id": thread_id,
        "post_id": None,
        "title": redact_personal_information(title)[:500],
        "excerpt": redact_personal_information(value.get("snippet") if isinstance(value.get("snippet"), str) else "")[:1200],
        "permalink": thread_url,
        "thread_url": thread_url,
        "author": redact_personal_information(author) or None,
        "created_time": _parse_datetime(created_at) or _parse_datetime(last_activity_at),
        "last_activity_time": _parse_datetime(last_activity_at),
        "forum": value.get("forum_name") if isinstance(value.get("forum_name"), str) else None,
        "replies": None,
        "views": None,
        "relevance": None,
        "media": [],
        "retrieval_provider": "bridge_api",
        "enriched_posts": 0,
    }


def _bridge_post_media(post: dict[str, Any]) -> list[dict[str, Any]]:
    media: list[dict[str, Any]] = []
    seen: set[str] = set()
    links = post.get("outbound_links")
    if not isinstance(links, list):
        return media
    for value in links:
        if not isinstance(value, str):
            continue
        normalized_media = _normalized_media_url(value)
        if not normalized_media:
            continue
        normalized, media_type = normalized_media
        if normalized in seen:
            continue
        seen.add(normalized)
        media.append(
            {
                "url": normalized,
                "type": media_type,
                "source_post_id": str(post.get("post_id") or "") or None,
                "source_post_permalink": post.get("permalink") if isinstance(post.get("permalink"), str) else None,
            }
        )
    return media


def _enrich_from_bridge_thread(result: dict[str, Any], payload: dict[str, Any], query: str) -> None:
    if str(payload.get("thread_id") or "") != result.get("thread_id"):
        return
    canonical_url = _verified_bridge_thread_url(payload.get("canonical_url"), result["thread_id"])
    if canonical_url:
        result["canonical_url"] = canonical_url
    if isinstance(payload.get("forum_name"), str):
        result["forum"] = payload["forum_name"]

    posts = [post for post in payload.get("posts", []) if isinstance(post, dict)] if isinstance(payload.get("posts"), list) else []
    result["enriched_posts"] = int(result.get("enriched_posts") or 0) + len(posts)
    query_lower = query.lower().lstrip("@").strip()
    best_post: dict[str, Any] | None = None
    best_key = tuple(result.get("_best_post_key") or (-1, -1, -1, -1, -1))
    merged_media = {
        entry["url"]: entry
        for entry in result.get("media", [])
        if isinstance(entry, dict) and isinstance(entry.get("url"), str)
    }
    for post in posts:
        text = post.get("text") if isinstance(post.get("text"), str) else ""
        post_media = _bridge_post_media(post)
        for entry in post_media:
            merged_media.setdefault(entry["url"], entry)
        occurrences = text.lower().count(query_lower) if query_lower else 0
        position = post.get("position") if isinstance(post.get("position"), int) else 0
        created_at = _parse_datetime(post.get("created_at") if isinstance(post.get("created_at"), str) else None)
        created_timestamp = created_at.timestamp() if created_at else 0.0
        if post_media:
            key = (2, created_timestamp, int(occurrences > 0), occurrences, position)
        elif occurrences:
            key = (1, occurrences, created_timestamp, position, 0)
        else:
            key = (0, created_timestamp, position, 0, 0)
        if key > best_key:
            best_key = key
            best_post = post

    result["media"] = list(merged_media.values())
    if not best_post:
        return
    result["_best_post_key"] = best_key
    text = best_post.get("text") if isinstance(best_post.get("text"), str) else ""
    if text:
        result["excerpt"] = redact_personal_information(text)[:1200]
    post_id = str(best_post.get("post_id") or "")
    permalink = best_post.get("permalink")
    if post_id.isdecimal() and isinstance(permalink, str):
        parsed_permalink = urlparse(permalink)
        thread_url = urlparse(result["thread_url"])
        if (
            parsed_permalink.scheme == "https"
            and parsed_permalink.netloc == thread_url.netloc
            and _id_from_patterns(permalink, THREAD_ID_PATTERNS) == result["thread_id"]
            and _id_from_patterns(permalink, POST_ID_PATTERNS) == post_id
        ):
            result["post_id"] = post_id
            result["permalink"] = permalink
    author = best_post.get("author_display_name") if isinstance(best_post.get("author_display_name"), str) else None
    created_at = best_post.get("created_at") if isinstance(best_post.get("created_at"), str) else None
    result["author"] = redact_personal_information(author) or result.get("author")
    result["created_time"] = _parse_datetime(created_at) or result.get("created_time")


async def _search_bridge(
    client: httpx.AsyncClient,
    state: _RequestState,
    bridge_url: str,
    request: KiwiFarmsCollectRequest,
    max_pages: int,
) -> tuple[list[dict[str, Any]], int, str | None]:
    results: list[dict[str, Any]] = []
    seen_results: set[str] = set()
    requests_used = 0
    page = 1

    search_request_cap = min(5, max(1, max_pages // 5))
    while len(results) < request.limit and requests_used < search_request_cap:
        page_limit = min(BRIDGE_SEARCH_LIMIT, request.limit - len(results))
        requests_used += 1
        payload = await _bridge_json(
            client,
            state,
            bridge_url,
            "/search",
            {"q": request.query.strip(), "page": page, "limit": page_limit},
        )
        values = payload.get("results")
        if not isinstance(values, list):
            raise PublicSearchUnavailable("the bridge search response omitted its results list", stop_fallbacks=True)
        for value in values:
            result = _bridge_search_result(value)
            if not result or result["external_id"] in seen_results:
                continue
            seen_results.add(result["external_id"])
            results.append(result)
            if len(results) >= request.limit:
                break
        if len(values) < page_limit:
            break
        page += 1

    if not results:
        return [], requests_used, "Kiwi Farms bridge search returned no verified results for that query."

    notes: list[str] = []
    enriched_results = 0
    revisit_candidates: list[tuple[dict[str, Any], int]] = []
    for result in results:
        if requests_used >= max_pages:
            break
        requests_used += 1
        try:
            payload = await _bridge_json(
                client,
                state,
                bridge_url,
                f"/threads/{result['thread_id']}",
                {"page": 1, "post_limit": BRIDGE_THREAD_POST_LIMIT},
            )
        except PublicSearchUnavailable as exc:
            notes.append(f"Thread {result['thread_id']} could not be enriched because {exc}.")
            continue
        _enrich_from_bridge_thread(result, payload, request.query)
        enriched_results += 1
        total_pages = payload.get("total_pages")
        if isinstance(total_pages, int) and total_pages > 1:
            revisit_candidates.append((result, total_pages))

    def revisit_priority(entry: tuple[dict[str, Any], int]) -> tuple[float, int]:
        activity = entry[0].get("last_activity_time") or entry[0].get("created_time")
        timestamp = activity.timestamp() if isinstance(activity, datetime) else 0.0
        return timestamp, int(not bool(entry[0].get("media")))

    revisit_candidates.sort(key=revisit_priority, reverse=True)
    for result, last_page in revisit_candidates:
        if requests_used >= max_pages:
            break
        requests_used += 1
        try:
            payload = await _bridge_json(
                client,
                state,
                bridge_url,
                f"/threads/{result['thread_id']}",
                {"page": last_page, "post_limit": BRIDGE_THREAD_POST_LIMIT},
            )
        except PublicSearchUnavailable as exc:
            notes.append(f"Recent page for thread {result['thread_id']} could not be enriched because {exc}.")
            continue
        _enrich_from_bridge_thread(result, payload, request.query)

    if enriched_results < len(results):
        notes.append(
            f"Media scan enriched {enriched_results} of {len(results)} search results before reaching the bridge request budget."
        )
    for result in results:
        result.pop("_best_post_key", None)
    return results, requests_used, " ".join(notes) or None


async def _search_base(
    client: httpx.AsyncClient,
    state: _RequestState,
    base_url: str,
    request: KiwiFarmsCollectRequest,
    max_pages: int,
) -> tuple[list[dict[str, Any]], int, str | None]:
    search_page = await state.request(client, "GET", urljoin(base_url.rstrip("/") + "/", "search/"))
    issue = _detect_access_issue(search_page)
    if issue:
        raise PublicSearchUnavailable(issue, stop_fallbacks="challenge" in issue or search_page.status_code in {403, 429})

    form = _extract_search_form(search_page.text, str(search_page.url))
    if not form:
        raise PublicSearchUnavailable("the public search form was unavailable or its HTML structure changed")
    action, method, keyword_field, fields = form
    fields[keyword_field] = request.query.strip()
    response = await state.request(client, method.upper(), action, params=fields if method == "get" else None, data=fields if method != "get" else None)
    issue = _detect_access_issue(response)
    if issue:
        raise PublicSearchUnavailable(issue, stop_fallbacks="challenge" in issue or response.status_code in {403, 429})

    pages_seen = 1
    results: list[dict[str, Any]] = []
    seen_results: set[str] = set()
    current = response
    while True:
        parsed = parse_search_results(current.text, str(current.url), request.limit - len(results))
        for result in parsed:
            if result["external_id"] not in seen_results:
                seen_results.add(result["external_id"])
                results.append(result)
        if len(results) >= request.limit or pages_seen >= max_pages:
            break
        next_url = _next_page_url(current.text, str(current.url))
        if not next_url:
            break
        current = await state.request(client, "GET", next_url)
        issue = _detect_access_issue(current)
        if issue:
            return results, pages_seen, f"Stopped pagination because {issue}."
        pages_seen += 1

    if not results:
        if _is_no_results(response.text):
            return [], pages_seen, "Kiwi Farms public search returned no results for that query."
        raise PublicSearchUnavailable("the search response contained no recognizable public results; the HTML structure may have changed")

    remaining_pages = max(0, max_pages - pages_seen)
    for result in results[:remaining_pages]:
        post_url = result.get("permalink")
        if not post_url:
            continue
        detail = await state.request(client, "GET", post_url)
        issue = _detect_access_issue(detail)
        if issue:
            continue
        pages_seen += 1
        _enrich_from_post_html(result, detail.text, str(detail.url))
    return results, pages_seen, None


def _source(db: Session, base_url: str) -> Source:
    source = db.query(Source).filter(Source.name == "kiwifarms").one_or_none()
    if not source:
        source = Source(name="kiwifarms", type="forum", base_url=base_url)
        db.add(source)
        db.flush()
    elif source.base_url != base_url:
        source.base_url = base_url
        db.flush()
    return source


def _upsert_result(db: Session, source: Source, result: dict[str, Any], query: str) -> Item:
    item = db.query(Item).filter(Item.source_id == source.id, Item.external_id == result["external_id"]).one_or_none()
    media = result.get("media") or []
    best_media = _best_media_url(media)
    title = result.get("title") or result.get("excerpt") or "Kiwi Farms public search result"
    excerpt = result.get("excerpt") or ""
    query_lower = query.lower().lstrip("@").strip()
    haystack = f"{title} {excerpt}".lower()
    query_occurrences = haystack.count(query_lower) if query_lower else 0
    metrics = {
        "reply_count": result.get("replies"),
        "view_count": result.get("views"),
        "forum": result.get("forum"),
        "embedded_media_count": len(media),
        "enriched_posts": result.get("enriched_posts", 0),
        "last_activity_at": result.get("last_activity_time"),
        "query_occurrences": query_occurrences,
        "result_relevance": result.get("relevance"),
    }
    parsed_metadata = {
        "thread_id": result.get("thread_id"),
        "post_id": result.get("post_id"),
        "thread_url": result.get("thread_url"),
        "canonical_url": result.get("canonical_url"),
        "post_permalink": result.get("permalink"),
        "forum": result.get("forum"),
        "query": redact_personal_information(query),
        "media": media,
        "retrieval_provider": result.get("retrieval_provider", "direct_public_web"),
    }
    permalink = result["permalink"]
    values = {
        "source_id": source.id,
        "external_id": result["external_id"],
        "title_or_text": redact_personal_information(title),
        "author_name": result.get("author"),
        "created_time": result.get("created_time"),
        "url": best_media or permalink,
        "permalink": permalink,
        "domain": urlparse(permalink).netloc,
        "self_text": redact_personal_information(excerpt),
        "is_video": bool(media),
        "is_reddit_hosted_video": False,
        "metrics_json": _json(metrics),
        "raw_json": _json(parsed_metadata),
        "deleted_or_removed": False,
    }
    if item:
        for key, value in values.items():
            setattr(item, key, value)
    else:
        item = Item(**values)
        db.add(item)
    db.flush()

    db.query(Media).filter(Media.item_id == item.id).delete()
    for index, entry in enumerate(media):
        db.add(
            Media(
                item_id=item.id,
                media_key=f"kiwifarms-{item.external_id}-{index}",
                media_type=entry["type"],
                url=entry["url"],
                raw_json=_json(
                    {
                        "discovered_on_public_forum_post": True,
                        "type": entry["type"],
                        "source_post_id": entry.get("source_post_id"),
                        "source_post_permalink": entry.get("source_post_permalink"),
                    }
                ),
            )
        )
    db.flush()
    rank_item(db, item)
    return item


def _nonfatal_note(reason: str, source_mode: str) -> str:
    access_description = "the public guest bridge" if source_mode == "bridge_api" else "the direct public guest flow"
    return (
        f"Kiwi Farms public search could not be accessed because {reason}. "
        f"Only {access_description} was used; no login or CAPTCHA bypass was attempted. "
        "Reddit and X collection can continue normally."
    )


async def collect_kiwifarms(db: Session, request: KiwiFarmsCollectRequest) -> dict[str, Any]:
    settings: Settings = get_settings()
    max_pages = min(request.max_pages or settings.kiwifarms_max_pages, 25)
    source_mode = "bridge_api" if settings.kiwifarms_bridge_configured else "public_web"
    run = Run(
        source="kiwifarms",
        mode=source_mode,
        status="running",
        raw_json=_json(
            {
                "query": redact_personal_information(request.query),
                "limit": request.limit,
                "max_pages": max_pages,
                "source_mode": source_mode,
            }
        ),
    )
    db.add(run)
    db.commit()

    state = _RequestState(max(0.0, settings.kiwifarms_request_delay_seconds))
    errors: list[str] = []
    pages_seen = 0
    timeout = settings.kiwifarms_bridge_timeout_seconds if source_mode == "bridge_api" else 20.0
    client_kwargs = web_client_kwargs(timeout=timeout, follow_redirects=True)
    client_kwargs["headers"] = {
        **DEFAULT_HEADERS,
        "Accept": "application/json" if source_mode == "bridge_api" else "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
    }
    try:
        async with httpx.AsyncClient(**client_kwargs) as client:
            results: list[dict[str, Any]] | None = None
            note: str | None = None
            used_base_url = settings.kiwifarms_base_url
            if source_mode == "bridge_api":
                results, pages_seen, note = await _search_bridge(
                    client,
                    state,
                    settings.kiwifarms_bridge_url,
                    request,
                    max_pages,
                )
                if results:
                    parsed_result_url = urlparse(results[0]["thread_url"])
                    used_base_url = f"{parsed_result_url.scheme}://{parsed_result_url.netloc}"
            else:
                for base_url in settings.kiwifarms_base_urls:
                    try:
                        results, pages_seen, note = await _search_base(client, state, base_url, request, max_pages)
                        used_base_url = base_url
                        break
                    except PublicSearchUnavailable as exc:
                        errors.append(str(exc))
                        if exc.stop_fallbacks:
                            raise
                    except (httpx.TimeoutException, httpx.NetworkError) as exc:
                        errors.append(f"the request to {urlparse(base_url).netloc} failed: {exc.__class__.__name__}")
            if results is None:
                raise PublicSearchUnavailable("; ".join(errors) or "no configured public hostname was reachable")

        source = _source(db, used_base_url)
        for result in results:
            _upsert_result(db, source, result, request.query)
        run = db.get(Run, run.id)
        run.status = "success"
        run.items_collected = len(results)
        run.finished_at = utcnow()
        db.commit()
        return {
            "run_id": run.id,
            "status": run.status,
            "source_mode": source_mode,
            "items_collected": len(results),
            "pages_seen": pages_seen,
            "note": note,
        }
    except Exception as exc:
        db.rollback()
        run = db.get(Run, run.id)
        reason = str(exc)
        if isinstance(exc, httpx.TimeoutException):
            reason = "the bridge request timed out" if source_mode == "bridge_api" else "the request timed out"
        elif isinstance(exc, httpx.NetworkError):
            reason = "the bridge hostname could not be reached" if source_mode == "bridge_api" else "the hostname could not be reached"
        elif isinstance(exc, httpx.HTTPError) and not reason:
            reason = "the public page request failed"
        note = _nonfatal_note(reason, source_mode)
        if run:
            run.status = "failed"
            run.finished_at = utcnow()
            run.error = note
            db.commit()
            return {
                "run_id": run.id,
                "status": run.status,
                "source_mode": source_mode,
                "items_collected": run.items_collected,
                "pages_seen": pages_seen,
                "note": note,
            }
        raise
