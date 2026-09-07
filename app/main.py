from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.config import get_settings
from app.database import init_db
from app.routers import (
    auth,
    contractor,
    inventory,
    invoices,
    machinery,
    media,
    payroll,
    petty_cash,
    piece_rate,
    reports,
    sites,
    tools,
    voice,
    worker,
)

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Development convenience only. In production AUTO_CREATE_TABLES is off and
    # the schema is created by a one-off job, because this runs once per
    # gunicorn worker and create_all never alters an existing table anyway.
    if settings.auto_create_tables:
        init_db()
    yield


app = FastAPI(
    title="Cashbook API",
    description="Attendance and cash ledger for contractors and workers.",
    version="0.1.0",
    lifespan=lifespan,
)

# The React Native app calls this API from a different origin.
allowed_origins = settings.cors_origin_list
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    # Auth rides on the Authorization header, not cookies, so credentials are
    # not needed — and "*" is not allowed alongside them.
    allow_credentials="*" not in allowed_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(contractor.router)
app.include_router(worker.router)
app.include_router(payroll.router)
app.include_router(sites.router)
# Site logistics (Phase 10). Registered after `sites` so the static
# /contractor/sites path is matched before these parameterised children.
app.include_router(petty_cash.router)
app.include_router(inventory.router)
app.include_router(tools.router)
app.include_router(piece_rate.router)
app.include_router(media.router)
app.include_router(voice.router)
app.include_router(reports.router)
# Phase 11. `machinery` is another parameterised child of /contractor/sites,
# so it follows `sites` for the same reason the Phase 10 routers do.
app.include_router(machinery.router)
app.include_router(invoices.router)


# Uploaded photos and voice notes, served straight off disk.
#
# Fine for one container behind one domain, which is what this deployment is.
# It is NOT a CDN: every image byte goes through a gunicorn worker that could
# be answering an API request instead. Put a reverse proxy in front of
# /media (nginx `alias`, or Cloudflare) before a site accumulates a few
# hundred photos, or move MEDIA_BACKEND to s3 — see app/services/storage.py.
#
# Only mounted for the local backend; with s3 the files are not on this box.
if settings.media_backend == "local":
    settings.media_path.mkdir(parents=True, exist_ok=True)
    app.mount(
        settings.media_url_prefix,
        StaticFiles(directory=settings.media_path),
        name="media",
    )


@app.get("/health", tags=["health"])
def health() -> dict[str, str]:
    return {"status": "ok"}
