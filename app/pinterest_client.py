import html
import json
import re
import unicodedata
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

import httpx

from app.web_headers import web_client_kwargs

PINTEREST_BASE_URL = "https://www.pinterest.com"
PINTEREST_SEARCH_RESOURCE_URL = f"{PINTEREST_BASE_URL}/resource/BaseSearchResource/get/"
PINTEREST_IMAGE_HOST = "i.pinimg.com"
MAX_PINTEREST_IMAGE_BYTES = 30 * 1024 * 1024
PINTEREST_IMAGE_EXTENSIONS = {
    "image/gif": ".gif",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}


@dataclass(frozen=True)
class PinterestPin:
    pin_id: str
    title: str
    description: str
    pin_url: str
    image_url: str
    width: int | None
    height: int | None
    pinner: str | None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def pinterest_query_slug(query: str) -> str:
    text = html.unescape(query)
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", text.lower()).strip(".-")
    cleaned = re.sub(r"-{2,}", "-", cleaned)
    return cleaned[:70].strip(".-") or "image-search"


def _valid_pinterest_image_url(value: Any) -> str | None:
    if not isinstance(value, str) or len(value) > 2048:
        return None
    parsed = urlparse(value)
    if parsed.scheme != "https" or (parsed.hostname or "").lower() != PINTEREST_IMAGE_HOST:
        return None
    return value


def _preferred_image(images: Any) -> tuple[str, int | None, int | None] | None:
    if not isinstance(images, dict):
        return None
    for size in ("orig", "736x", "564x", "474x", "236x", "170x"):
        image = images.get(size)
        if not isinstance(image, dict):
            continue
        image_url = _valid_pinterest_image_url(image.get("url"))
        if image_url:
            width = image.get("width") if isinstance(image.get("width"), int) else None
            height = image.get("height") if isinstance(image.get("height"), int) else None
            return image_url, width, height
    return None


def _pin_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        for key in ("text", "format"):
            if isinstance(value.get(key), str):
                return value[key].strip()
    return ""


def parse_pinterest_search_response(payload: Any, limit: int) -> list[PinterestPin]:
    if not isinstance(payload, dict):
        raise ValueError("Pinterest returned an invalid search response")
    resource = payload.get("resource_response")
    if not isinstance(resource, dict):
        raise ValueError("Pinterest search data was missing from the response")
    if resource.get("http_status") not in {None, 200} or resource.get("status") not in {None, "success"}:
        raise ValueError(str(resource.get("message") or "Pinterest search failed"))
    data = resource.get("data")
    results = data.get("results") if isinstance(data, dict) else None
    if not isinstance(results, list):
        raise ValueError("Pinterest did not return a list of public pins")

    pins: list[PinterestPin] = []
    seen: set[str] = set()
    for result in results:
        if not isinstance(result, dict) or result.get("type") != "pin":
            continue
        pin_id = str(result.get("id") or "").strip()
        preferred = _preferred_image(result.get("images"))
        if not pin_id.isdigit() or not preferred:
            continue
        image_url, width, height = preferred
        if image_url in seen:
            continue
        seen.add(image_url)
        pinner_data = result.get("pinner")
        pinner = None
        if isinstance(pinner_data, dict):
            pinner = str(pinner_data.get("username") or pinner_data.get("full_name") or "").strip() or None
        pins.append(
            PinterestPin(
                pin_id=pin_id,
                title=_pin_text(result.get("grid_title")) or _pin_text(result.get("title")),
                description=(
                    _pin_text(result.get("description"))
                    or _pin_text(result.get("seo_alt_text"))
                    or _pin_text(result.get("auto_alt_text"))
                ),
                pin_url=f"{PINTEREST_BASE_URL}/pin/{pin_id}/",
                image_url=image_url,
                width=width,
                height=height,
                pinner=pinner,
            )
        )
        if len(pins) >= limit:
            break
    return pins


async def search_public_pinterest(query: str, limit: int = 8) -> tuple[str, list[PinterestPin]]:
    clean_query = " ".join(query.split()).strip()
    if not clean_query:
        raise ValueError("Enter a description of the Pinterest images to find")
    if len(clean_query) > 300:
        raise ValueError("Pinterest image requests must be 300 characters or fewer")
    limit = max(1, min(int(limit), 20))
    source_url = f"/search/pins/?q={quote(clean_query, safe='')}"
    search_url = f"{PINTEREST_BASE_URL}{source_url}"

    async with httpx.AsyncClient(**web_client_kwargs(timeout=45.0, follow_redirects=True)) as client:
        page = await client.get(
            search_url,
            headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                "Referer": f"{PINTEREST_BASE_URL}/",
            },
        )
        page.raise_for_status()
        csrf_token = client.cookies.get("csrftoken")
        if not csrf_token:
            raise ValueError("Pinterest did not start a public search session")

        request_data = json.dumps(
            {
                "options": {
                    "query": clean_query,
                    "scope": "pins",
                    "page_size": 25,
                    "enable_promoted_pins": False,
                },
                "context": {},
            },
            separators=(",", ":"),
        )
        response = await client.post(
            PINTEREST_SEARCH_RESOURCE_URL,
            data={"source_url": source_url, "data": request_data, "rs": "typed"},
            headers={
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "Origin": PINTEREST_BASE_URL,
                "Referer": str(page.url),
                "X-CSRFToken": csrf_token,
                "X-Pinterest-AppState": "active",
                "X-Pinterest-Source-Url": source_url,
                "X-Requested-With": "XMLHttpRequest",
            },
        )
        response.raise_for_status()
        try:
            payload = response.json()
        except json.JSONDecodeError as exc:
            raise ValueError("Pinterest returned a non-JSON search response") from exc
    return search_url, parse_pinterest_search_response(payload, limit)


def _pinterest_image_extension(content_type: str, final_url: str) -> str:
    media_type = content_type.split(";", 1)[0].strip().lower()
    if media_type in PINTEREST_IMAGE_EXTENSIONS:
        return PINTEREST_IMAGE_EXTENSIONS[media_type]
    suffix = Path(urlparse(final_url).path).suffix.lower()
    if suffix in {".gif", ".jpeg", ".jpg", ".png", ".webp"}:
        return ".jpg" if suffix == ".jpeg" else suffix
    raise ValueError(f"Pinterest returned an unsupported image type ({media_type or 'unknown'})")


async def download_pinterest_pin(pin: PinterestPin, destination_stem: Path) -> Path:
    if not _valid_pinterest_image_url(pin.image_url):
        raise ValueError("Refusing an image URL outside Pinterest's image host")
    async with httpx.AsyncClient(**web_client_kwargs(timeout=60.0, follow_redirects=True)) as client:
        response = await client.get(
            pin.image_url,
            headers={
                "Accept": "image/webp,image/png,image/jpeg,image/gif,*/*;q=0.8",
                "Referer": pin.pin_url,
            },
        )
        response.raise_for_status()
    if not _valid_pinterest_image_url(str(response.url)):
        raise ValueError("Pinterest redirected the image outside its image host")
    if not response.content:
        raise ValueError("Pinterest returned an empty image")
    if len(response.content) > MAX_PINTEREST_IMAGE_BYTES:
        raise ValueError(f"Pinterest image is larger than the {MAX_PINTEREST_IMAGE_BYTES // (1024 * 1024)} MB limit")
    extension = _pinterest_image_extension(response.headers.get("content-type", ""), str(response.url))
    destination = destination_stem.with_suffix(extension)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(f"{extension}.part")
    temporary.write_bytes(response.content)
    temporary.replace(destination)
    return destination
