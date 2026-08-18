from fastapi import APIRouter
from app.config import settings

router = APIRouter()


@router.get("/api/v1/health")
def health():
    return {
        "status": "ok",
        "environment": settings.environment,
        "llm_mode": "mock" if settings.llm_mock_mode else "live",
    }
