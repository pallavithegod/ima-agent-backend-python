from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, EmailStr, Field
from slowapi import Limiter
from slowapi.util import get_remote_address

from ..db import repo
from ..security import current_user, issue_token
from ..services.credentials import upsert_connection
from ..services.crypto import hash_password, verify_password
from ..services.firebase import verify_firebase_token

router = APIRouter(prefix="/api/auth", tags=["auth"])
limiter = Limiter(key_func=get_remote_address)

AUTH_RATE_LIMIT = "100/15 minutes"


class RegisterPayload(BaseModel):
    name: str = Field(min_length=2, max_length=80)
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)


class LoginPayload(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)


class FirebasePayload(BaseModel):
    idToken: str = Field(min_length=20)
    githubAccessToken: str = Field(min_length=10)


@router.post("/register", status_code=201)
@limiter.limit(AUTH_RATE_LIMIT)
def register(request: Request, payload: RegisterPayload) -> dict[str, Any]:
    email = payload.email.lower()
    if repo.get_user_by_email(email):
        raise HTTPException(status_code=409, detail="Email is already registered")
    user = repo.create_user(payload.name, email, hash_password(payload.password))
    return {"token": issue_token(user), "user": repo.safe_user(user)}


@router.post("/login")
@limiter.limit(AUTH_RATE_LIMIT)
def login(request: Request, payload: LoginPayload) -> dict[str, Any]:
    user = repo.get_user_by_email(payload.email.lower())
    if not user or not verify_password(payload.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Incorrect email or password")
    return {"token": issue_token(user), "user": repo.safe_user(user)}


@router.post("/firebase")
@limiter.limit(AUTH_RATE_LIMIT)
def firebase_login(request: Request, payload: FirebasePayload) -> dict[str, Any]:
    try:
        identity = verify_firebase_token(payload.idToken)
    except RuntimeError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(status_code=401, detail="Firebase token is invalid") from error
    email = identity.get("email")
    if not email:
        raise HTTPException(status_code=400, detail="GitHub must provide an email address")
    name = identity.get("name") or email.split("@")[0]
    user = repo.upsert_firebase_user(name, email.lower(), identity["uid"])
    github_identities = (identity.get("firebase") or {}).get("identities") or {}
    account_id = (github_identities.get("github.com") or [identity["uid"]])[0]
    upsert_connection(user["id"], "github", payload.githubAccessToken, str(account_id), name, {})
    return {"token": issue_token(user), "user": repo.safe_user(user)}


@router.get("/me")
def me(user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    record = repo.get_user_by_id(user["sub"])
    if not record:
        raise HTTPException(status_code=404, detail="User not found")
    return {"user": repo.safe_user(record)}
