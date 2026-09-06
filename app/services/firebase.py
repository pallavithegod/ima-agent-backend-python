"""Firebase Admin verification of GitHub sign-in ID tokens. The Firestore
credential mirror from the Node backend was intentionally dropped."""

import base64
import json
from pathlib import Path
from threading import Lock
from typing import Any

from ..config import BACKEND_ROOT, get_settings

_lock = Lock()
_app = None


def _initialize():
    global _app
    import firebase_admin
    from firebase_admin import credentials

    settings = get_settings()
    if settings.firebase_service_account_base64:
        info = json.loads(base64.b64decode(settings.firebase_service_account_base64))
        credential = credentials.Certificate(info)
    else:
        path = Path(settings.firebase_service_account_path)
        if not path.is_absolute():
            path = BACKEND_ROOT / path
        if not path.exists():
            raise RuntimeError(
                "Firebase sign-in is not configured. Set FIREBASE_SERVICE_ACCOUNT_BASE64 "
                "or provide a service-account.json file."
            )
        credential = credentials.Certificate(str(path))
    _app = firebase_admin.initialize_app(credential)
    return _app


def verify_firebase_token(id_token: str) -> dict[str, Any]:
    """Verify a Firebase ID token and return the decoded identity claims."""
    from firebase_admin import auth

    with _lock:
        if _app is None:
            _initialize()
    return auth.verify_id_token(id_token, check_revoked=True)
