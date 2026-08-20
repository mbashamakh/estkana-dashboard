from __future__ import annotations

import bcrypt
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import User
from app.db.session import get_db
from app.auth.session import create_session_token, read_session_token

router = APIRouter()
settings = get_settings()


class LoginRequest(BaseModel):
    email: str
    password: str


@router.post("/api/login")
def login(body: LoginRequest, response: Response, db: Session = Depends(get_db)):
    user = db.scalar(select(User).where(User.email == body.email.lower().strip(), User.is_active == True))  # noqa: E712
    if not user or not user.password_hash:
        raise HTTPException(status_code=401, detail="Invalid email or password")
    if not bcrypt.checkpw(body.password.encode(), user.password_hash.encode()):
        raise HTTPException(status_code=401, detail="Invalid email or password")

    token = create_session_token(user.id, user.email)
    response.set_cookie(
        key=settings.session_cookie_name,
        value=token,
        max_age=settings.session_max_age_seconds,
        httponly=True,
        samesite="lax",
        secure=settings.environment == "production",
    )
    return {"email": user.email, "is_admin": user.is_admin}


@router.post("/api/logout")
def logout(response: Response):
    response.delete_cookie(settings.session_cookie_name)
    return {"ok": True}


def get_current_user(request: Request, db: Session = Depends(get_db)) -> User:
    """FastAPI dependency — raises 401 if there's no valid session cookie."""
    token = request.cookies.get(settings.session_cookie_name)
    if not token:
        raise HTTPException(status_code=401, detail="Not logged in")
    data = read_session_token(token)
    if not data:
        raise HTTPException(status_code=401, detail="Session expired")
    user = db.get(User, data["uid"])
    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="Account disabled")
    return user
