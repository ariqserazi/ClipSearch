import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import Item, Ranking, Source


def _load_json(value: str, fallback):
    try:
        return json.loads(value or "")
    except json.JSONDecodeError:
        return fallback


def latest_rankings(db: Session, limit: int = 25, source: str = "all"):
    subquery = (
        db.query(Ranking.item_id, func.max(Ranking.id).label("ranking_id"))
        .group_by(Ranking.item_id)
        .subquery()
    )
    query = (
        db.query(Item, Ranking)
        .join(subquery, subquery.c.item_id == Item.id)
        .join(Ranking, Ranking.id == subquery.c.ranking_id)
        .join(Source, Source.id == Item.source_id)
        .filter(Item.deleted_or_removed.is_(False))
    )
    if source == "reddit_x":
        query = query.filter(Source.name.in_(("reddit", "x")))
    elif source != "all":
        query = query.filter(Source.name == source)
    return query.order_by(Ranking.drama_score.desc(), Item.created_time.desc()).limit(limit).all()


def markdown_report(db: Session, limit: int = 25, source: str = "all") -> str:
    rows = latest_rankings(db, limit, source)
    generated = datetime.now(timezone.utc).isoformat()
    lines = [
        "# Drama Clip Scout Latest Report",
        "",
        f"Source filter: {source}",
        "",
        f"Generated: {generated}",
        "",
        "These are potential leads ranked from public metadata. Review the linked source before making any claim.",
        "",
    ]
    if not rows:
        lines.append("No clips collected yet.")
        return "\n".join(lines) + "\n"

    for index, (item, ranking) in enumerate(rows, start=1):
        metrics = _load_json(item.metrics_json, {})
        source = item.source.name if item.source else "unknown"
        created = item.created_time.isoformat() if item.created_time else "unknown"
        lines.extend(
            [
                f"## {index}. {item.title_or_text[:180]}",
                "",
                f"- Source: {source}",
                f"- Link: {item.url}",
                *([f"- Forum post: {item.permalink}"] if item.permalink and item.permalink != item.url else []),
                f"- Score: {ranking.drama_score}",
                f"- Label: {ranking.potential_label}",
                f"- Reason: {ranking.reasoning}",
                f"- Created: {created}",
                f"- Metrics: `{json.dumps(metrics, ensure_ascii=True)}`",
                "",
            ]
        )
    return "\n".join(lines)


def since_for_window(time_window: str):
    now = datetime.now(timezone.utc)
    if time_window == "day":
        return now - timedelta(days=1)
    if time_window == "week":
        return now - timedelta(days=7)
    if time_window == "month":
        return now - timedelta(days=30)
    if time_window == "year":
        return datetime(now.year, 1, 1, tzinfo=timezone.utc)
    return None
