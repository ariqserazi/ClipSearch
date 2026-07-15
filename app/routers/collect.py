from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.database import get_db
from app.reddit_client import collect_reddit
from app.schemas import CollectAllRequest, RedditCollectRequest, XArchiveImportRequest, XArchiveSearchRequest, XCollectRequest, XFromRedditRequest
from app.x_client import collect_x, collect_x_archive, collect_x_from_archive_search, collect_x_from_reddit

router = APIRouter()


@router.post("/collect/reddit")
async def collect_reddit_endpoint(request: RedditCollectRequest | None = None, db: Session = Depends(get_db)):
    return await collect_reddit(db, request or RedditCollectRequest())


@router.post("/collect/x")
async def collect_x_endpoint(request: XCollectRequest | None = None, db: Session = Depends(get_db)):
    return await collect_x(db, request or XCollectRequest())


@router.post("/collect/x/from-reddit")
async def collect_x_from_reddit_endpoint(request: XFromRedditRequest | None = None, db: Session = Depends(get_db)):
    return await collect_x_from_reddit(db, request or XFromRedditRequest())


@router.post("/collect/x/from-archive-search")
async def collect_x_from_archive_search_endpoint(request: XArchiveSearchRequest, db: Session = Depends(get_db)):
    return await collect_x_from_archive_search(db, request)


@router.post("/collect/x/from-web-search")
async def collect_x_from_web_search_endpoint(request: XArchiveSearchRequest, db: Session = Depends(get_db)):
    request.search_provider = "web"
    return await collect_x_from_archive_search(db, request)


@router.post("/collect/x/from-google-search")
async def collect_x_from_legacy_google_search_endpoint(request: XArchiveSearchRequest, db: Session = Depends(get_db)):
    request.search_provider = "web"
    return await collect_x_from_archive_search(db, request)


@router.post("/collect/x/archive")
async def collect_x_archive_endpoint(request: XArchiveImportRequest, db: Session = Depends(get_db)):
    return await collect_x_archive(db, request)


@router.post("/collect/all")
async def collect_all_endpoint(request: CollectAllRequest | None = None, db: Session = Depends(get_db)):
    payload = request or CollectAllRequest()
    reddit_result = await collect_reddit(db, payload.reddit)
    x_result = await collect_x(db, payload.x)
    return {"reddit": reddit_result, "x": x_result}
