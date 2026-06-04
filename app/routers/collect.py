from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.database import get_db
from app.reddit_client import collect_reddit
from app.schemas import CollectAllRequest, RedditCollectRequest, XCollectRequest
from app.x_client import collect_x

router = APIRouter()


@router.post("/collect/reddit")
async def collect_reddit_endpoint(request: RedditCollectRequest | None = None, db: Session = Depends(get_db)):
    return await collect_reddit(db, request or RedditCollectRequest())


@router.post("/collect/x")
async def collect_x_endpoint(request: XCollectRequest | None = None, db: Session = Depends(get_db)):
    return await collect_x(db, request or XCollectRequest())


@router.post("/collect/all")
async def collect_all_endpoint(request: CollectAllRequest | None = None, db: Session = Depends(get_db)):
    payload = request or CollectAllRequest()
    reddit_result = await collect_reddit(db, payload.reddit)
    x_result = await collect_x(db, payload.x)
    return {"reddit": reddit_result, "x": x_result}
