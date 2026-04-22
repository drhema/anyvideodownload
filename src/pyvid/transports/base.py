from __future__ import annotations

from pathlib import Path
from typing import Protocol

from playwright.async_api import BrowserContext

from ..core.types import Candidate, DownloadResult


class Transport(Protocol):
    async def download(
        self,
        candidate: Candidate,
        ctx: BrowserContext,
        output_dir: Path,
    ) -> DownloadResult: ...


_HEADER_DENY_PREFIXES = (":", "sec-fetch", "sec-ch-ua")
_HEADER_DENY_EXACT = {
    "host", "content-length", "connection", "transfer-encoding",
    "accept-encoding", "upgrade", "upgrade-insecure-requests",
    "te", "trailer", "proxy-authorization", "proxy-authenticate",
}


def _sanitize(headers: dict[str, str]) -> dict[str, str]:
    """Strip hop-by-hop and HTTP/2 pseudo-headers that httpx manages itself."""
    out = {}
    for k, v in headers.items():
        kl = k.lower()
        if kl in _HEADER_DENY_EXACT:
            continue
        if any(kl.startswith(p) for p in _HEADER_DENY_PREFIXES):
            continue
        out[k] = v
    return out


async def session_headers(ctx: BrowserContext, url: str, extra: dict[str, str] | None = None) -> dict[str, str]:
    """Headers suitable for fetching `url` using the browser's cookie jar."""
    cookies = await ctx.cookies(url)
    cookie_header = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
    h: dict[str, str] = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
    }
    if cookie_header:
        h["Cookie"] = cookie_header
    if extra:
        h.update(_sanitize(extra))
    return _sanitize(h)
