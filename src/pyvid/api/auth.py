"""Bearer-token auth.

Tokens are read from env var PYVID_API_TOKENS as a comma-separated list.
If the list is empty, auth is disabled (all requests pass as "anonymous").
For production: always set tokens.
"""
from __future__ import annotations

import os

from fastapi import Header, HTTPException


def _load_tokens() -> set[str]:
    raw = os.environ.get("PYVID_API_TOKENS", "")
    return {t.strip() for t in raw.split(",") if t.strip()}


TOKENS = _load_tokens()


def require_token(authorization: str | None = Header(default=None)) -> str:
    if not TOKENS:
        return "anonymous"
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing Authorization: Bearer <token>")
    token = authorization.split(None, 1)[1].strip()
    if token not in TOKENS:
        raise HTTPException(status_code=401, detail="Invalid token")
    return token
