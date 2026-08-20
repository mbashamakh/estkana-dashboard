from __future__ import annotations

from fastapi import APIRouter, Depends

from app.auth.routes import get_current_user
from app.db.models import User

router = APIRouter()


@router.get("/api/me")
def get_me(user: User = Depends(get_current_user)):
    return {"email": user.email, "is_admin": user.is_admin}
