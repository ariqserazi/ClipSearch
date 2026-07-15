import asyncio
import html
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Item

router = APIRouter()

DOWNLOAD_ROOT = Path("/data/downloads")
DOWNLOAD_TIMEOUT_SECONDS = 900


@dataclass
class DownloadProcessResult:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False


def _safe_segment(value: str | None, fallback: str, *, max_length: int = 80, lowercase: bool = False) -> str:
    text = html.unescape(value or "")
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    if lowercase:
        text = text.lower()
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", text).strip(".-")
    cleaned = re.sub(r"-{2,}", "-", cleaned)
    return cleaned[:max_length].strip(".-") or fallback


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
        "size_bytes": path.stat().st_size,
    }


def _downloaded_files(target_dir: Path, download_root: Path = DOWNLOAD_ROOT) -> list[dict[str, Any]]:
    if not target_dir.exists():
        return []
    files = [path for path in target_dir.rglob("*") if path.is_file()]
    files.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    return [_file_info(path, download_root) for path in files[:20]]


async def _run_ytdlp(url: str, target_dir: Path, timeout_seconds: int = DOWNLOAD_TIMEOUT_SECONDS) -> DownloadProcessResult:
    command = [
        "yt-dlp",
        "--no-playlist",
        "--restrict-filenames",
        "--trim-filenames",
        "180",
        "--merge-output-format",
        "mp4",
        "-P",
        str(target_dir),
        "-o",
        "%(title).120B [%(id)s].%(ext)s",
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
    result = await _run_ytdlp(item.url, target_dir, timeout_seconds)
    status = "success" if result.returncode == 0 else "failed"
    return {
        "status": status,
        "item_id": item.id,
        "source": item.source.name if item.source else "unknown",
        "url": item.url,
        "download_dir": str(target_dir),
        "host_dir": str(Path("./data/downloads") / target_dir.relative_to(download_root)),
        "files": _downloaded_files(target_dir, download_root),
        "returncode": result.returncode,
        "timed_out": result.timed_out,
        "stdout_tail": _tail(result.stdout),
        "stderr_tail": _tail(result.stderr),
    }


@router.post("/downloads/items/{item_id}")
async def download_item_endpoint(item_id: int, db: Session = Depends(get_db)):
    return await download_item_media(db, item_id)
