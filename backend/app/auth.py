"""Authentication and role-based authorisation.

Sanctions screening output is confidential: it names partners suspected of a
regulatory nexus. The application therefore requires authentication on every
endpoint except login and health, and gates watchlist mutation behind an admin
role.
"""

from __future__ import annotations

import datetime as dt
import secrets

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from passlib.context import CryptContext
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .config import get_settings
from .database import get_db
from .models import User

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
# auto_error=False so an absent token reaches current_user as None, which
# single-operator mode needs in order to skip authentication entirely.
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login", auto_error=False)

ROLES = ("admin", "analyst", "viewer")

# Placeholder hashes for accounts that must never be signed into: the synthetic
# single-operator record exists only to give audit events a real actor.
UNUSABLE_PASSWORDS = ("", "!")


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


def create_access_token(user: User) -> str:
    settings = get_settings()
    now = dt.datetime.now(dt.UTC)
    payload = {
        "sub": user.email,
        "role": user.role,
        "region": user.region,
        "iat": now,
        "exp": now + dt.timedelta(minutes=settings.access_token_ttl_minutes),
    }
    return jwt.encode(payload, settings.secret_key, algorithm="HS256")


def authenticate(db: Session, email: str, password: str) -> User | None:
    user = db.execute(
        select(User).where(func.lower(User.email) == email.strip().lower())
    ).scalar_one_or_none()
    if user is None or not user.is_active or user.hashed_password in UNUSABLE_PASSWORDS:
        # Burn equivalent time so a missing account — or the synthetic
        # single-operator record — is not distinguishable from a wrong password
        # by response latency.
        pwd_context.dummy_verify()
        return None
    if not verify_password(password, user.hashed_password):
        return None
    return user


def local_operator(db: Session) -> User:
    """Get-or-create the synthetic account used in single-operator mode.

    A real row is used rather than a detached object so audit events carry a
    genuine actor and behave identically to a multi-user deployment.
    """
    settings = get_settings()
    email = settings.local_operator.strip().lower()
    user = db.execute(
        select(User).where(func.lower(User.email) == email)
    ).scalar_one_or_none()
    if user is None:
        user = User(
            email=email,
            # Unusable password: this account cannot be logged into normally.
            hashed_password="!",
            full_name="Local Operator",
            role="admin",
        )
        db.add(user)
        db.commit()
        db.refresh(user)
    return user


def current_user(
    token: str | None = Depends(oauth2_scheme), db: Session = Depends(get_db)
) -> User:
    settings = get_settings()

    if settings.single_user_mode:
        return local_operator(db)

    credentials_error = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or expired credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    if not token:
        raise credentials_error
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=["HS256"])
    except jwt.PyJWTError:
        raise credentials_error from None

    email = payload.get("sub")
    if not email:
        raise credentials_error

    user = db.execute(
        select(User).where(func.lower(User.email) == str(email).lower())
    ).scalar_one_or_none()
    if user is None or not user.is_active:
        raise credentials_error
    return user


def require_role(*allowed: str):
    """Dependency factory enforcing a role allowlist."""

    def dependency(user: User = Depends(current_user)) -> User:
        if user.role not in allowed:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Requires one of roles: {', '.join(allowed)}",
            )
        return user

    return dependency


def ensure_bootstrap_admin(db: Session) -> str | None:
    """Create the first administrator if the user table is empty.

    Returns the generated password when one was generated, so the caller can
    surface it once at startup. Never stores or logs it afterwards.
    """
    settings = get_settings()
    if settings.single_user_mode:
        # No login screen is shown, so a bootstrap credential would be noise.
        return None

    # Count only accounts that can actually log in.
    #
    # Single-operator mode creates a synthetic `local_operator` record with an
    # unusable password so audit events have a real actor. Counting it as an
    # "existing user" suppressed bootstrap on the switch to multi-user mode,
    # which locked the operator out of their own instance with no recovery
    # path — the one account present could not be signed into.
    existing = db.execute(
        select(func.count(User.id)).where(
            User.hashed_password.notin_(UNUSABLE_PASSWORDS)
        )
    ).scalar_one()
    if existing:
        return None

    generated: str | None = None
    password = settings.bootstrap_admin_password
    if not password:
        password = secrets.token_urlsafe(16)
        generated = password

    db.add(
        User(
            email=settings.bootstrap_admin_email.strip().lower(),
            hashed_password=hash_password(password),
            full_name="Bootstrap Administrator",
            role="admin",
        )
    )
    db.commit()
    return generated
