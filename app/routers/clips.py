import json
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Comment, Item, Ranking, Run, Source
from app.ranking import rank_all
from app.reports import since_for_window

router = APIRouter()
UNVERIFIED_WEB_SEARCH_MARKER = "free_web_search_discovered"


def load_json(value: str, fallback):
    try:
        return json.loads(value or "")
    except json.JSONDecodeError:
        return fallback


def latest_ranking_subquery(db: Session):
    return db.query(Ranking.item_id, func.max(Ranking.id).label("ranking_id")).group_by(Ranking.item_id).subquery()


def serialize_item(item: Item, ranking: Ranking | None = None, include_raw: bool = False) -> dict[str, Any]:
    source_name = item.source.name if item.source else "unknown"
    latest = ranking or (item.rankings[-1] if item.rankings else None)
    is_video = bool(item.is_video and UNVERIFIED_WEB_SEARCH_MARKER not in (item.raw_json or ""))
    media = [
        {
            "type": entry.media_type,
            "url": entry.url,
            "preview_image_url": entry.preview_image_url,
            "duration_ms": entry.duration_ms,
            "width": entry.width,
            "height": entry.height,
            "variants": load_json(entry.variants_json, []),
        }
        for entry in item.media
    ]
    result = {
        "id": item.id,
        "title_or_text": item.title_or_text,
        "source": source_name,
        "url": item.url,
        "permalink": item.permalink,
        "thumbnail": item.thumbnail,
        "created_time": item.created_time.isoformat() if item.created_time else None,
        "collected_at": item.collected_at.isoformat() if item.collected_at else None,
        "author_name": item.author_name,
        "subreddit": item.subreddit,
        "domain": item.domain,
        "flair": item.flair,
        "is_video": is_video,
        "metrics": load_json(item.metrics_json, {}),
        "media": {"items": media, "has_video": is_video},
        "drama_score": latest.drama_score if latest else 0,
        "potential_label": latest.potential_label if latest else "low potential",
        "reasoning": latest.reasoning if latest else "Not ranked yet.",
        "why_it_may_be_useful": latest.reasoning if latest else "No ranking signals are available yet.",
    }
    if include_raw:
        result["self_text"] = item.self_text
        result["comments"] = [
            {
                "author_name": comment.author_name,
                "body": comment.body,
                "score": comment.score,
                "created_time": comment.created_time.isoformat() if comment.created_time else None,
            }
            for comment in item.comments
        ]
        result["raw_metadata"] = load_json(item.raw_json, {})
    return result


def query_ranked_items(
    db: Session,
    source: str = "all",
    min_drama_score: float = 0,
    time_window: str = "week",
    has_video: bool | None = None,
    keyword: str | None = None,
    account: str | None = None,
    limit: int = 50,
):
    latest = latest_ranking_subquery(db)
    query = (
        db.query(Item, Ranking)
        .join(latest, latest.c.item_id == Item.id)
        .join(Ranking, Ranking.id == latest.c.ranking_id)
        .join(Source, Source.id == Item.source_id)
        .filter(Item.deleted_or_removed.is_(False), Ranking.drama_score >= min_drama_score)
    )
    if source == "reddit_x":
        query = query.filter(Source.name.in_(("reddit", "x")))
    elif source != "all":
        query = query.filter(Source.name == source)
    since = since_for_window(time_window)
    if since:
        query = query.filter(or_(Item.created_time.is_(None), Item.created_time >= since))
    if has_video is not None:
        unverified_web_search_lead = Item.raw_json.ilike(f"%{UNVERIFIED_WEB_SEARCH_MARKER}%")
        if has_video:
            query = query.filter(Item.is_video.is_(True), ~unverified_web_search_lead)
        else:
            query = query.filter(or_(Item.is_video.is_(False), unverified_web_search_lead))
    if account:
        clean_account = account.strip().lstrip("@")
        if clean_account:
            account_url_pattern = f"%/{clean_account}/status/%"
            x_account_match = or_(
                Item.author_name.ilike(clean_account),
                Item.url.ilike(account_url_pattern),
                Item.permalink.ilike(account_url_pattern),
            )
            if source == "x":
                query = query.filter(x_account_match)
            elif source in {"all", "reddit_x"}:
                query = query.filter(or_(Source.name != "x", x_account_match))
    if keyword:
        tokens = [part.strip() for part in keyword.split() if part.strip()]
        patterns = [f"%{token}%" for token in tokens] or [f"%{keyword}%"]
        query = query.filter(
            or_(
                *[
                    condition
                    for pattern in patterns
                    for condition in (
                        Item.title_or_text.ilike(pattern),
                        and_(Source.name != "x", Item.self_text.ilike(pattern)),
                    )
                ]
            )
        )
    return query.order_by(Ranking.drama_score.desc(), Item.created_time.desc()).limit(limit).all()


@router.get("/clips")
def list_clips(
    source: str = Query("all", pattern="^(reddit|x|reddit_x|kiwifarms|all)$"),
    min_drama_score: float = Query(0, ge=0, le=100),
    time_window: str = Query("week", pattern="^(day|week|month|year|all)$"),
    has_video: bool | None = None,
    keyword: str | None = None,
    account: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
):
    rows = query_ranked_items(db, source, min_drama_score, time_window, has_video, keyword, account, limit)
    return {"results": [serialize_item(item, ranking) for item, ranking in rows]}


@router.get("/clips/{item_id}")
def get_clip(item_id: int, db: Session = Depends(get_db)):
    latest = latest_ranking_subquery(db)
    row = (
        db.query(Item, Ranking)
        .join(latest, latest.c.item_id == Item.id)
        .join(Ranking, Ranking.id == latest.c.ranking_id)
        .filter(Item.id == item_id)
        .one_or_none()
    )
    if not row:
        item = db.get(Item, item_id)
        if not item:
            raise HTTPException(status_code=404, detail="Clip not found")
        return serialize_item(item, None, include_raw=True)
    item, ranking = row
    return serialize_item(item, ranking, include_raw=True)


@router.get("/sources")
def list_sources(db: Session = Depends(get_db)):
    sources = db.query(Source).all()
    return {
        "sources": [
            {
                "id": source.id,
                "name": source.name,
                "type": source.type,
                "base_url": source.base_url,
                "created_at": source.created_at.isoformat() if source.created_at else None,
            }
            for source in sources
        ]
    }


@router.get("/runs")
def list_runs(limit: int = Query(50, ge=1, le=200), db: Session = Depends(get_db)):
    runs = db.query(Run).order_by(Run.started_at.desc()).limit(limit).all()
    return {
        "runs": [
            {
                "id": run.id,
                "source": run.source,
                "mode": run.mode,
                "started_at": run.started_at.isoformat() if run.started_at else None,
                "finished_at": run.finished_at.isoformat() if run.finished_at else None,
                "status": run.status,
                "items_collected": run.items_collected,
                "error": run.error,
            }
            for run in runs
        ]
    }


@router.post("/rank")
def rank_endpoint(db: Session = Depends(get_db)):
    count = rank_all(db)
    return {"status": "success", "items_ranked": count, "ranked_at": datetime.now(timezone.utc).isoformat()}
