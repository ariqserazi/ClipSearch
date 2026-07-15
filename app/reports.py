import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import Item, Ranking


def _load_json(value: str, fallback):
    try:
        return json.loads(value or "")
    except json.JSONDecodeError:
        return fallback


def latest_rankings(db: Session, limit: int = 25):
    subquery = (
        db.query(Ranking.item_id, func.max(Ranking.id).label("ranking_id"))
        .group_by(Ranking.item_id)
        .subquery()
    )
    return (
        db.query(Item, Ranking)
        .join(subquery, subquery.c.item_id == Item.id)
        .join(Ranking, Ranking.id == subquery.c.ranking_id)
        .filter(Item.deleted_or_removed.is_(False))
        .order_by(Ranking.drama_score.desc(), Item.created_time.desc())
        .limit(limit)
        .all()
    )


def markdown_report(db: Session, limit: int = 25) -> str:
    rows = latest_rankings(db, limit)
    generated = datetime.now(timezone.utc).isoformat()
    lines = [
        "# Drama Clip Scout Latest Report",
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
