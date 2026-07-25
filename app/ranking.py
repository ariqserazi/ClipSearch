import json
import math
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Comment, Item, Ranking


KEYWORDS = [
    "drama",
    "called out",
    "exposed",
    "banned",
    "response",
    "apology",
    "leaked",
    "crashout",
    "beef",
    "clip",
    "vod",
    "streamer",
]

INTENSITY_WORDS = [
    "wild",
    "insane",
    "lying",
    "proof",
    "receipts",
    "context",
    "ban",
    "scam",
    "fake",
    "response",
    "apologize",
    "explain",
]


def _load_json(value: str, fallback):
    try:
        return json.loads(value or "")
    except json.JSONDecodeError:
        return fallback


def _age_hours(created_time: datetime | None) -> float:
    if not created_time:
        return 168.0
    if created_time.tzinfo is None:
        created_time = created_time.replace(tzinfo=timezone.utc)
    return max((datetime.now(timezone.utc) - created_time).total_seconds() / 3600, 0.1)


def _label(score: float) -> str:
    if score >= 70:
        return "high potential"
    if score >= 40:
        return "medium potential"
    return "low potential"


def rank_item(db: Session, item: Item) -> Ranking:
    settings = get_settings()
    metrics = _load_json(item.metrics_json, {})
    text = f"{item.title_or_text or ''} {item.self_text or ''}".lower()
    comments = db.query(Comment).filter(Comment.item_id == item.id).all()
    comment_text = " ".join(comment.body.lower() for comment in comments[:10])

    score_value = float(metrics.get("score") or metrics.get("like_count") or metrics.get("retweet_count") or 0)
    comment_count = float(metrics.get("num_comments") or metrics.get("reply_count") or metrics.get("replies") or 0)
    view_count = float(metrics.get("view_count") or metrics.get("views") or 0)
    query_occurrences = float(metrics.get("query_occurrences") or 0)
    embedded_media_count = float(metrics.get("embedded_media_count") or len(item.media) or 0)
    upvote_ratio = float(metrics.get("upvote_ratio") or 0)
    age_hours = _age_hours(item.created_time)

    engagement_points = min(28.0, math.log1p(max(score_value, 0)) * 4.0)
    comment_points = min(24.0, math.log1p(max(comment_count, 0)) * 5.0)
    velocity_points = min(18.0, (comment_count / age_hours) * 3.0)
    keyword_hits = [kw for kw in KEYWORDS if kw in text]
    keyword_points = min(18.0, len(keyword_hits) * 4.0)
    video_points = 12.0 if item.is_video or item.media else 0.0
    recent_points = max(0.0, 10.0 - (age_hours / 12.0))
    streamer_hits = [name for name in settings.streamers if name and name in text]
    streamer_points = min(8.0, len(streamer_hits) * 4.0)
    comment_intensity_hits = [word for word in INTENSITY_WORDS if word in comment_text]
    comment_intensity_points = min(12.0, len(comment_intensity_hits) * 2.0)
    ratio_points = 4.0 if upvote_ratio >= 0.85 else 0.0
    view_points = min(8.0, math.log1p(max(view_count, 0)) * 0.8)
    relevance_points = min(12.0, max(query_occurrences, 0) * 3.0)
    multiple_media_points = min(4.0, max(embedded_media_count - 1, 0) * 2.0)

    total = min(
        100.0,
        engagement_points
        + comment_points
        + velocity_points
        + keyword_points
        + video_points
        + recent_points
        + streamer_points
        + comment_intensity_points
        + ratio_points
        + view_points
        + relevance_points
        + multiple_media_points,
    )

    reasons: list[str] = []
    if comment_count:
        label = "replies" if item.source and item.source.type == "forum" else "comments"
        reasons.append(f"{int(comment_count)} {label}")
    if view_count:
        reasons.append(f"{int(view_count)} views")
    if score_value:
        reasons.append(f"engagement score {int(score_value)}")
    if velocity_points >= 6:
        reasons.append("active recent discussion")
    if keyword_hits:
        reasons.append("matched keywords: " + ", ".join(keyword_hits[:4]))
    if item.is_video or item.media:
        reasons.append("has video or media metadata")
    if embedded_media_count > 1:
        reasons.append(f"{int(embedded_media_count)} independent media links")
    if query_occurrences:
        reasons.append(f"query matched {int(query_occurrences)} time{'s' if query_occurrences != 1 else ''}")
    if streamer_hits:
        reasons.append("matched configured streamer names")
    if comment_intensity_hits:
        reasons.append("top comments contain intense discussion terms")
    if not reasons:
        reasons.append("limited signals so far")

    ranking = Ranking(
        item_id=item.id,
        drama_score=round(total, 2),
        potential_label=_label(total),
        reasoning="; ".join(reasons) + ". Treat as a lead to review, not confirmed drama.",
    )
    db.add(ranking)
    db.flush()
    return ranking


def rank_all(db: Session) -> int:
    count = 0
    for item in db.query(Item).filter(Item.deleted_or_removed.is_(False)).all():
        rank_item(db, item)
        count += 1
    db.commit()
    return count
