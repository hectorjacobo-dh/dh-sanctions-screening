"""Authentication and user administration."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..audit import record_event
from ..auth import (
    authenticate,
    create_access_token,
    current_user,
    hash_password,
    require_role,
)
from ..database import get_db
from ..models import User
from ..schemas import Token, UserCreate, UserOut

router = APIRouter(prefix="/api", tags=["auth"])


@router.post("/auth/login", response_model=Token)
def login(
    form: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)
) -> Token:
    user = authenticate(db, form.username, form.password)
    if user is None:
        record_event(
            db,
            "LOGIN_FAILED",
            actor=form.username.strip().lower()[:320],
            entity_ref="auth",
        )
        raise HTTPException(401, "Incorrect email or password")

    record_event(db, "LOGIN_SUCCEEDED", actor=user.email, entity_ref="auth")
    return Token(
        access_token=create_access_token(user),
        email=user.email,
        role=user.role,
        full_name=user.full_name,
    )


@router.get("/auth/me", response_model=UserOut)
def me(user: User = Depends(current_user)) -> User:
    return user


@router.get("/users", response_model=list[UserOut])
def list_users(
    db: Session = Depends(get_db), _: User = Depends(require_role("admin"))
) -> list[User]:
    return list(db.execute(select(User).order_by(User.id)).scalars())


@router.post("/users", response_model=UserOut, status_code=201)
def create_user(
    body: UserCreate,
    db: Session = Depends(get_db),
    admin: User = Depends(require_role("admin")),
) -> User:
    email = str(body.email).strip().lower()
    exists = db.execute(
        select(func.count(User.id)).where(func.lower(User.email) == email)
    ).scalar_one()
    if exists:
        raise HTTPException(409, "A user with that email already exists")

    user = User(
        email=email,
        hashed_password=hash_password(body.password),
        full_name=body.full_name,
        role=body.role,
        region=body.region,
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    record_event(
        db,
        "USER_CREATED",
        actor=admin.email,
        entity_ref=email,
        payload={"role": user.role, "region": user.region},
    )
    return user


@router.post("/users/{user_id}/deactivate", response_model=UserOut)
def deactivate_user(
    user_id: int,
    db: Session = Depends(get_db),
    admin: User = Depends(require_role("admin")),
) -> User:
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(404, "User not found")
    if user.id == admin.id:
        raise HTTPException(400, "You cannot deactivate your own account")

    user.is_active = False
    db.commit()
    record_event(db, "USER_DEACTIVATED", actor=admin.email, entity_ref=user.email)
    db.refresh(user)
    return user
