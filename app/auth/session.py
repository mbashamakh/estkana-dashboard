"""
Signed cookie sessions via itsdangerous, following Starlette's own
SessionMiddleware pattern rather than hand-rolling token generation/expiry —
per the architecture review, rolling your own is an unnecessary risk class
for near-zero benefit at this scale.

Auth mechanism note: this currently implements email+password with an
admin-managed allowlist (no self-signup). If the user's domain turns out to
be Google Workspace-managed, swap this for Google Sign-In restricted to that
domain instead — it removes password storage entirely and "admin grants
access" becomes "admin adds an email," which is simpler to operate. Ask
before building more on top of the password path.
"""
from __future__ import annotations

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app.config import get_settings

settings = get_settings()
_serializer = URLSafeTimedSerializer(settings.session_secret, salt="estkana-session")


def create_session_token(user_id: int, email: str) -> str:
    return _serializer.dumps({"uid": user_id, "email": email})


def read_session_token(token: str) -> dict | None:
    try:
        return _serializer.loads(token, max_age=settings.session_max_age_seconds)
    except (BadSignature, SignatureExpired):
        return None
