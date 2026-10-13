"""Shared FastAPI dependencies: DB sessions and ops-token authorization."""

from __future__ import annotations

import hmac
from typing import Any

from fastapi import HTTPException

from driftline.core.config import get_settings
from driftline.persistence import database


def get_db() -> Any:
    yield from database.get_session()


def ops_token_ok(provided: str | None) -> bool:
    """Shared-token ops protection; open only when no token is configured.

    CORS is never treated as authentication. When ``DRIFTLINE_OPS_TOKEN`` is
    set, privileged endpoints require it via ``X-Driftline-Ops-Token`` (or
    ``Authorization: Bearer``). When unset, endpoints are open for local
    development only — deploy behind network controls and set the token.
    Comparison is timing-safe (hmac.compare_digest).
    """
    expected = (get_settings().ops_token or "").strip()
    if not expected:
        return True
    if not provided:
        return False
    token = provided.strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    return hmac.compare_digest(token, expected)


def require_ops_token(request: Any) -> None:
    provided = request.headers.get("x-driftline-ops-token") or request.headers.get("authorization")
    if not ops_token_ok(provided):
        raise HTTPException(status_code=401, detail="missing or invalid ops token")
