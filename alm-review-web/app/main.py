from contextlib import asynccontextmanager
from urllib.parse import urlparse
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles

from app.config import PROJECT_DIR, get_settings
from app.database import Base, SessionLocal, engine
from app.migrations import ensure_compatible_schema
from app.services.defaults import ensure_defaults
from app.services.review_policy import (
    adopt_legacy_workspace_policies,
    backfill_legacy_review_policy,
)
from app.services.scheduler import create_scheduler
from app.services.worker_lease import acquire_worker_lease, release_worker_lease
from app.services.worker_tasks import current_worker_id
from app.web import router


@asynccontextmanager
async def lifespan(application: FastAPI):
    Base.metadata.create_all(engine)
    ensure_compatible_schema(engine)
    owner_token = str(uuid4())
    owns_lease = False
    with SessionLocal() as db:
        ensure_defaults(db)
        backfill_legacy_review_policy(db)
        adopt_legacy_workspace_policies(db)
        settings = get_settings()
        if settings.app_role == "combined" and settings.scheduler_enabled:
            owns_lease = acquire_worker_lease(
                db,
                owner_token,
                current_worker_id(),
                settings.worker_singleton_lease_seconds,
            )
    scheduler = create_scheduler(owner_token) if owns_lease else None
    application.state.scheduler = scheduler
    application.state.worker_owner_token = owner_token if owns_lease else None
    if scheduler is not None:
        scheduler.start()
    try:
        yield
    finally:
        if scheduler is not None:
            scheduler.shutdown(wait=False)
        if owns_lease:
            with SessionLocal() as db:
                release_worker_lease(db, owner_token)


app = FastAPI(title="ALM Review Workspace", lifespan=lifespan)


@app.middleware("http")
async def reject_cross_origin_writes(request: Request, call_next):
    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        origin = request.headers.get("origin")
        request_host = request.headers.get("host", "").casefold()
        if origin and urlparse(origin).netloc.casefold() != request_host:
            return PlainTextResponse("Cross-origin request rejected.", status_code=403)
    return await call_next(request)


app.mount("/static", StaticFiles(directory=PROJECT_DIR / "app" / "static"), name="static")
app.include_router(router)