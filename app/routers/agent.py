from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.database import get_db
from app.routers.clips import query_ranked_items, serialize_item
from app.schemas import AgentSearchRequest, AgentSearchResponse, ClipResult

router = APIRouter()


@router.post("/agent/search-clips", response_model=AgentSearchResponse)
def agent_search_clips(request: AgentSearchRequest, db: Session = Depends(get_db)):
    keyword = " ".join(request.keywords).strip() or None
    rows = query_ranked_items(
        db=db,
        source=request.source,
        min_drama_score=request.min_drama_score,
        time_window=request.time_window,
        keyword=keyword,
        limit=request.limit,
    )
    results = []
    for item, ranking in rows:
        serialized = serialize_item(item, ranking)
        results.append(
            ClipResult(
                id=serialized["id"],
                title_or_text=serialized["title_or_text"],
                source=serialized["source"],
                url=serialized["url"],
                thumbnail=serialized["thumbnail"],
                drama_score=serialized["drama_score"],
                potential_label=serialized["potential_label"],
                reasoning=serialized["reasoning"],
                created_time=serialized["created_time"],
                metrics=serialized["metrics"],
                media=serialized["media"],
                why_it_may_be_useful=serialized["why_it_may_be_useful"],
            )
        )
    return AgentSearchResponse(results=results)
