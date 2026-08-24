"""
FastAPI application entrypoint.

Startup does two idempotent things so a fresh deploy is immediately usable:
  1. Creates tables if they don't exist yet (Base.metadata.create_all —
     fine for this stage; switch to Alembic migrations once the schema is
     stabilizing and multiple environments need to stay in sync).
  2. Bootstraps a single admin user from ADMIN_EMAIL / ADMIN_PASSWORD env
     vars, ONLY if the users table is empty. This exists purely to solve
     the chicken-and-egg problem of a login-gated app with no signup route
     and zero rows in `users` on first deploy. Additional users are created
     by an admin from within the app (see app/api/admin.py), not via env
     vars, going forward.
"""
from __future__ import annotations

import os

import bcrypt
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select

from app.api.me import router as me_router
from app.api.pnl import router as pnl_router
from app.api.diag import router as diag_router
from app.auth.routes import router as auth_router
from app.config import get_settings
from app.db.migrate import run_startup_migrations
from app.db.models import User  # noqa: F401 -- ensures model is registered on Base
from app.db.session import Base, SessionLocal, engine

settings = get_settings()
app = FastAPI(title="Estkana Outlet Dashboard API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if settings.environment != "production" else [],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router)
app.include_router(pnl_router)
app.include_router(me_router)
app.include_router(diag_router)


@app.on_event("startup")
def on_startup() -> None:
    run_startup_migrations(engine)
    Base.metadata.create_all(bind=engine)

    admin_email = os.getenv("ADMIN_EMAIL")
    admin_password = os.getenv("ADMIN_PASSWORD")
    if not (admin_email and admin_password):
        return

    db = SessionLocal()
    try:
        has_any_user = db.scalar(select(User).limit(1)) is not None
        if has_any_user:
            return
        pw_hash = bcrypt.hashpw(admin_password.encode(), bcrypt.gensalt()).decode()
        db.add(User(email=admin_email.lower().strip(), password_hash=pw_hash, is_admin=True, is_active=True))
        db.commit()
    finally:
        db.close()


@app.get("/api/health")
def health():
    return {"status": "ok", "schema_version": settings.schema_version}


# --- Static frontend ---
# Serves the dashboard's static assets. index.html itself does a client-side
# auth check (GET /api/me) and redirects to /login.html if not authenticated.
STATIC_DIR = os.path.join(os.path.dirname(__file__), "..", "static")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
def root():
    return FileResponse(os.path.join(STATIC_DIR, "dashboard.html"))


@app.get("/login.html")
def login_page():
    return FileResponse(os.path.join(STATIC_DIR, "login.html"))
