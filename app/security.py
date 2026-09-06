from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .config import get_settings

JWT_AUDIENCE = "incident-memory-api"
JWT_ISSUER = "incident-memory-auth"

bearer = HTTPBearer(auto_error=False)


def issue_token(user: dict[str, Any]) -> str:
    now = datetime.now(UTC)
    return jwt.encode(
        {
            "sub": str(user["id"]),
            "email": user["email"],
            "name": user["name"],
            "role": user["role"],
            "iat": now,
            "exp": now + timedelta(hours=12),
            "aud": JWT_AUDIENCE,
            "iss": JWT_ISSUER,
        },
        get_settings().auth_jwt_secret,
        algorithm="HS256",
    )


def current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
) -> dict[str, Any]:
    if credentials is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    try:
        payload = jwt.decode(
            credentials.credentials,
            get_settings().auth_jwt_secret,
            algorithms=["HS256"],
            audience=JWT_AUDIENCE,
            issuer=JWT_ISSUER,
        )
    except jwt.PyJWTError as error:
        raise HTTPException(status_code=401, detail="Invalid or expired token") from error
    return payload
