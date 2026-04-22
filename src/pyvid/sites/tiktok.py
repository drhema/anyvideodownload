"""TikTok site module — extracts unwatermarked MP4 URL from page state.

Strategy
--------
TikTok embeds a JSON blob in `<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__">`
with full video metadata. The web's `playAddr` (and the entries in
`bitrateInfo[].PlayAddr.UrlList`) are **unwatermarked** on desktop web; only
the mobile-app "Save" path applies the TikTok watermark via `downloadAddr`.

So we:
  1. After the page loads in Playwright, read that script tag's JSON.
  2. Walk to itemStruct.video → pick the highest-bitrate rendition.
  3. Return its URL as a progressive Candidate with Referer: https://www.tiktok.com/.

Caveats
-------
- URLs are signed and expire in minutes — download must happen in-session.
- Short URLs (vm.tiktok.com/xxx) are handled by Playwright following redirects;
  the canonical @user/video/<id> page is what we parse.
- TOS: this is for personal / archival use. Don't ship as a public scraper
  without thinking through the legal side (see README notes).
"""
from __future__ import annotations

import json
import sys
from typing import Any

from ..core.session import SessionState
from ..core.types import Candidate


async def choose_candidate(_candidates: list[Candidate], session: SessionState) -> Candidate | None:
    page = session.page
    try:
        blob = await page.evaluate(
            "document.getElementById('__UNIVERSAL_DATA_FOR_REHYDRATION__')?.textContent || ''"
        )
    except Exception as e:
        print(f"[tiktok] could not read page JSON: {e}", file=sys.stderr)
        return None

    if not blob:
        print("[tiktok] __UNIVERSAL_DATA_FOR_REHYDRATION__ not found on page; "
              "is this a real TikTok video URL?", file=sys.stderr)
        return None

    try:
        data = json.loads(blob)
    except json.JSONDecodeError as e:
        print(f"[tiktok] failed to parse page JSON: {e}", file=sys.stderr)
        return None

    video = _walk(data, ["__DEFAULT_SCOPE__", "webapp.video-detail", "itemInfo", "itemStruct", "video"])
    if not isinstance(video, dict):
        # Fallback for older/newer schemas
        video = _find_first(data, "video") if isinstance(data, dict) else None
    if not isinstance(video, dict):
        print("[tiktok] couldn't locate video metadata in page JSON", file=sys.stderr)
        return None

    best_url, best_bitrate, best_meta = _pick_best_url(video)
    if not best_url:
        print("[tiktok] no playable URL found in video metadata", file=sys.stderr)
        return None

    size_hint = int(video.get("size") or 0)
    duration = int(video.get("duration") or 0)
    width = int(video.get("width") or 0)
    height = int(video.get("height") or 0)
    print(
        f"[tiktok] picked {width}x{height} @ {best_bitrate // 1000}kbps "
        f"({duration}s, {size_hint // 1024}KB)  via {best_meta}",
        file=sys.stderr,
    )

    return Candidate(
        kind="progressive",
        url=best_url,
        content_type="video/mp4",
        size_hint=size_hint,
        request_headers={"Referer": "https://www.tiktok.com/"},
        notes=f"tiktok webapp unwatermarked {width}x{height}",
    )


def _walk(obj: Any, path: list[str]) -> Any:
    cur = obj
    for key in path:
        if not isinstance(cur, dict) or key not in cur:
            return None
        cur = cur[key]
    return cur


def _find_first(obj: Any, key: str, depth: int = 0) -> Any:
    """Recursive depth-limited search for a key with dict value."""
    if depth > 6 or not isinstance(obj, dict):
        return None
    if key in obj and isinstance(obj[key], dict):
        return obj[key]
    for v in obj.values():
        if isinstance(v, dict):
            found = _find_first(v, key, depth + 1)
            if found is not None:
                return found
        elif isinstance(v, list):
            for item in v:
                found = _find_first(item, key, depth + 1)
                if found is not None:
                    return found
    return None


def _pick_best_url(video: dict) -> tuple[str, int, str]:
    """Return (url, bitrate, source_description). Prefer bitrateInfo, then playAddr."""
    candidates: list[tuple[str, int, str]] = []

    for b in video.get("bitrateInfo") or []:
        pa = b.get("PlayAddr") or b.get("playAddr") or {}
        url_list = pa.get("UrlList") or pa.get("urlList") or []
        bitrate = int(b.get("Bitrate") or b.get("bitrate") or 0)
        for url in url_list:
            if url and not _looks_watermarked(url):
                candidates.append((url, bitrate, f"bitrateInfo {bitrate // 1000}kbps"))

    if candidates:
        return max(candidates, key=lambda t: t[1])

    # Fallbacks in order of preference: playAddr > downloadAddr (latter may have logo)
    for key, label in [("playAddr", "playAddr"), ("downloadAddr", "downloadAddr (may include watermark)")]:
        url = video.get(key)
        if url and not _looks_watermarked(url):
            return url, 0, label
    # If only watermarked URLs exist, still return something so the user sees output.
    for key, label in [("playAddr", "playAddr"), ("downloadAddr", "downloadAddr (WATERMARKED fallback)")]:
        url = video.get(key)
        if url:
            return url, 0, label
    return "", 0, ""


def _looks_watermarked(url: str) -> bool:
    """Heuristic: TikTok 'download' CDN URLs embed 'watermark' in the path or query."""
    ul = url.lower()
    return "watermark=1" in ul or "with_watermark" in ul
