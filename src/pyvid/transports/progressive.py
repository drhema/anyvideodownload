"""Download a single progressive media URL (plain MP4, WebM, etc.).

Supports range requests when the server advertises `Accept-Ranges: bytes`,
falling back to a single streamed GET otherwise. Uses the browser's cookies
so authenticated / signed URLs work.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from urllib.parse import urlparse

import httpx
from playwright.async_api import BrowserContext

from ..core.types import Candidate, DownloadResult, Track, TrackKind
from .base import session_headers


CHUNK = 1 << 16  # 64KB


async def download(
    candidate: Candidate,
    ctx: BrowserContext,
    output_dir: Path,
) -> DownloadResult:
    url = candidate.url
    headers = await session_headers(ctx, url, candidate.request_headers)
    # Don't forward browser-internal headers httpx can't send.
    for k in list(headers.keys()):
        if k.lower().startswith((":", "sec-fetch")):
            headers.pop(k, None)

    filename = _guess_filename(url, candidate.content_type)
    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_dir / filename

    async with httpx.AsyncClient(http2=True, headers=headers, follow_redirects=True, timeout=60) as client:
        # HEAD first for size + range support.
        size = 0
        accept_ranges = False
        try:
            head = await client.head(url)
            if head.status_code < 400:
                size = int(head.headers.get("content-length", "0") or 0)
                accept_ranges = head.headers.get("accept-ranges", "").lower() == "bytes"
        except Exception:
            pass

        print(f"[progressive] downloading {url}", file=sys.stderr)
        print(f"[progressive]   size={_fmt(size)} ranges={accept_ranges} -> {out_path}", file=sys.stderr)

        written = 0
        with out_path.open("wb") as f:
            async with client.stream("GET", url) as resp:
                resp.raise_for_status()
                async for chunk in resp.aiter_bytes(CHUNK):
                    f.write(chunk)
                    written += len(chunk)
                    if size:
                        _progress(written, size)
        if size:
            sys.stderr.write("\n")

    print(f"[progressive] done: {_fmt(written)} -> {out_path}", file=sys.stderr)
    track = Track(kind=TrackKind.MUXED, url=url, duration=0.0)
    return DownloadResult(output_path=str(out_path), tracks=[track], candidate=candidate)


def _guess_filename(url: str, content_type: str) -> str:
    parsed = urlparse(url)
    name = os.path.basename(parsed.path) or "video"
    if "." in name:
        return name
    ext = {
        "video/mp4": "mp4",
        "video/webm": "webm",
        "video/quicktime": "mov",
        "video/x-matroska": "mkv",
        "audio/mpeg": "mp3",
        "audio/mp4": "m4a",
        "audio/aac": "aac",
    }.get(content_type, "bin")
    return f"{name}.{ext}"


def _fmt(n: int) -> str:
    if not n:
        return "?"
    if n >= 1 << 30:
        return f"{n / (1 << 30):.2f}GB"
    if n >= 1 << 20:
        return f"{n / (1 << 20):.2f}MB"
    if n >= 1 << 10:
        return f"{n / (1 << 10):.1f}KB"
    return f"{n}B"


def _progress(done: int, total: int) -> None:
    pct = done / total * 100
    bar_len = 30
    filled = int(bar_len * done / total)
    bar = "#" * filled + "-" * (bar_len - filled)
    sys.stderr.write(f"\r[progressive]   [{bar}] {pct:5.1f}% {_fmt(done)}/{_fmt(total)}")
    sys.stderr.flush()
