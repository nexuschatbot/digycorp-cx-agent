from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings

app = FastAPI(title=settings.app_name)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health() -> dict[str, str]:
    """Liveness probe. The deploy workflow polls this after restarting the service."""
    return {"status": "ok", "environment": settings.environment}


@app.get("/")
def root() -> dict[str, str]:
    return {"service": settings.app_name}
