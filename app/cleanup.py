from sqlalchemy.orm import Session

from app.models import Item


REMOVED_MARKERS = {"[deleted]", "[removed]"}


def mark_removed_content(db: Session) -> int:
    changed = 0
    for item in db.query(Item).all():
        title = (item.title_or_text or "").strip().lower()
        body = (item.self_text or "").strip().lower()
        if title in REMOVED_MARKERS or body in REMOVED_MARKERS:
            if not item.deleted_or_removed:
                item.deleted_or_removed = True
                changed += 1
    db.commit()
    return changed
