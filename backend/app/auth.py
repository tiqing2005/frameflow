from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .config import Settings
from .models import AuthSession

COOKIE_NAME = "frameflow_session"
PASSWORD_ITERATIONS = 310_000


def hash_password(password: str, *, iterations: int = PASSWORD_ITERATIONS) -> str:
    """Return a portable PBKDF2-SHA256 password hash for environment config."""
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return "$".join(
        (
            "pbkdf2_sha256",
            str(iterations),
            base64.urlsafe_b64encode(salt).decode("ascii").rstrip("="),
            base64.urlsafe_b64encode(digest).decode("ascii").rstrip("="),
        )
    )


def _decode_b64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations_raw, salt_raw, digest_raw = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        iterations = int(iterations_raw)
        if iterations < 100_000 or iterations > 2_000_000:
            return False
        actual = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), _decode_b64(salt_raw), iterations
        )
        return hmac.compare_digest(actual, _decode_b64(digest_raw))
    except (TypeError, ValueError):
        return False


def credentials_configured(settings: Settings) -> bool:
    return bool(settings.auth_password_hash or settings.auth_password)


def authenticate(settings: Settings, username: str, password: str) -> bool:
    username_ok = hmac.compare_digest(username, settings.auth_username)
    if settings.auth_password_hash:
        password_ok = verify_password(password, settings.auth_password_hash)
    elif settings.auth_password:
        password_ok = hmac.compare_digest(password, settings.auth_password)
    else:
        password_ok = False
    # Evaluate both checks before returning to reduce username enumeration signal.
    return username_ok and password_ok


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def create_session(db: Session, settings: Settings) -> tuple[AuthSession, str]:
    now = datetime.now(timezone.utc)
    db.execute(delete(AuthSession).where(AuthSession.expires_at <= now))
    token = secrets.token_urlsafe(32)
    session = AuthSession(
        username=settings.auth_username,
        token_hash=token_hash(token),
        csrf_token=secrets.token_urlsafe(24),
        expires_at=now + timedelta(hours=settings.auth_session_hours),
        last_seen_at=now,
    )
    db.add(session)
    db.flush()
    return session, token


def find_session(db: Session, token: str | None) -> AuthSession | None:
    if not token:
        return None
    session = db.scalar(select(AuthSession).where(AuthSession.token_hash == token_hash(token)))
    if session is None:
        return None
    now = datetime.now(timezone.utc)
    if _utc(session.expires_at) <= now:
        db.delete(session)
        db.commit()
        return None
    # Avoid turning every API call or video range request into a SQLite write.
    if _utc(session.last_seen_at) <= now - timedelta(minutes=5):
        session.last_seen_at = now
        db.commit()
    return session


def delete_session(db: Session, token: str | None) -> None:
    if token:
        db.execute(delete(AuthSession).where(AuthSession.token_hash == token_hash(token)))
        db.commit()
