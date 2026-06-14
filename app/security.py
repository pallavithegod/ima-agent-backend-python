from typing import Any

import jwt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .config import get_settings


bearer = HTTPBearer(auto_error=False)


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
            audience="incident-memory-api",
            issuer="incident-memory-auth",
        )
    except jwt.PyJWTError as error:
        raise HTTPException(status_code=401, detail="Invalid or expired token") from error
    return payload

