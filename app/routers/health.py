from fastapi import APIRouter

from app.config import get_settings

router = APIRouter()


@router.get("/health")
def health():
    settings = get_settings()
    return {
        "status": "ok",
        "service": "drama-clip-scout",
        "reddit_configured": settings.reddit_configured,
        "x_configured": settings.x_configured,
    }
