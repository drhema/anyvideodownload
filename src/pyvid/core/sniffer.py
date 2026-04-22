"""Classifies captured network traffic into Candidate streams.

Runs after the session closes. Walks the recorded CapturedRequest list and
emits a Candidate for each plausible stream — HLS manifest, DASH manifest,
progressive media URL, or YouTube UMP endpoint. The orchestrator picks among
them.
"""
from __future__ import annotations

import re
from collections.abc import Iterable
from urllib.parse import urlparse

from .types import Candidate, CapturedRequest

# These regexes are applied to URL *path only* (not query), so `?url=x.mpd`
# in a player page doesn't trigger a false match.
MEDIA_EXT_RE = re.compile(
    r"\.(mp4|m4s|m4a|m4v|mov|webm|mkv|ogg|ogv|mp3|aac|wav|flac)$",
    re.IGNORECASE,
)
HLS_EXT_RE = re.compile(r"\.m3u8$", re.IGNORECASE)
DASH_EXT_RE = re.compile(r"\.mpd$", re.IGNORECASE)
UMP_PATH_RE = re.compile(r"googlevideo\.com/videoplayback", re.IGNORECASE)
# Vimeo's proprietary adaptive: host vod-adaptive*.vimeocdn.com + path ends in /playlist.json
VIMEO_PATH_RE = re.compile(r"vod-adaptive[^/]*\.vimeocdn\.com/.*/playlist\.json", re.IGNORECASE)
# Vimeo per-segment range fetches — suppress these as progressive candidates since they're slices.
VIMEO_SEGMENT_RE = re.compile(r"vimeocdn\.com/.*/range/prot/.*\.mp4", re.IGNORECASE)

PROGRESSIVE_MIME_RE = re.compile(r"^(video|audio)/", re.IGNORECASE)
HLS_MIME_RE = re.compile(r"(application/(vnd\.apple\.mpegurl|x-mpegurl))", re.IGNORECASE)
DASH_MIME_RE = re.compile(r"application/dash\+xml", re.IGNORECASE)
UMP_MIME_RE = re.compile(r"application/vnd\.yt-ump", re.IGNORECASE)


def classify(captures: Iterable[CapturedRequest]) -> list[Candidate]:
    """Return candidates, deduplicated by URL, ordered by descending score."""
    by_url: dict[str, Candidate] = {}

    for cap in captures:
        cand = _classify_one(cap)
        if cand is None:
            continue
        prev = by_url.get(cand.url)
        if prev is None or cand.score() > prev.score():
            by_url[cand.url] = cand

    return sorted(by_url.values(), key=lambda c: c.score(), reverse=True)


def _classify_one(cap: CapturedRequest) -> Candidate | None:
    url = cap.url
    url_path = urlparse(url).path
    ct = cap.content_type
    size = cap.size
    hdrs = cap.request_headers

    # If the response was a range/partial (status 206 or has Content-Range),
    # it's almost certainly an HLS/DASH segment fetch, not a standalone file.
    # Let the manifest candidates represent it instead.
    is_range = cap.status == 206 or bool(cap.headers.get("content-range"))

    # Vimeo adaptive (playlist.json on vod-adaptive*.vimeocdn.com)
    if VIMEO_PATH_RE.search(url):
        return Candidate(kind="vimeo", url=url, content_type=ct, size_hint=size,
                         request_headers=hdrs, notes="vimeo playlist.json")

    # HLS manifest
    if HLS_EXT_RE.search(url_path) or HLS_MIME_RE.search(ct):
        # Score bias: master > unlabeled > media rendition > iframe.
        # Match against url_path only — query strings often contain misleading
        # tokens (e.g. `player_backend=mediaplayer`).
        pl = url_path.lower()
        bias = 0
        if "master" in pl:
            bias = 10_000_000   # hard prefer
        elif re.search(r"[_/](audio|video|subtitle|caption|cc)[_/]", pl) or \
             re.search(r"_\d{3,}(kbps|k|_|\.m3u8)", pl) or \
             re.search(r"_(stereo|mono|surround)(_|\.)", pl):
            bias = -100_000_000  # hard deprioritize sub-manifests
        elif "iframe" in pl:
            bias = -1_000_000_000  # effectively disqualified
        elif "prog_index" in pl or "/playlist/" in pl:
            # Twitch's euc11.playlist.ttvnw.net/v1/playlist/... is a MEDIA
            # (already-resolved rendition) playlist; prefer the /usher/ master.
            bias = -5_000_000
        return Candidate(kind="hls", url=url, content_type=ct, size_hint=size + bias,
                         request_headers=hdrs, notes="hls manifest")

    # DASH manifest
    if DASH_EXT_RE.search(url_path) or DASH_MIME_RE.search(ct):
        return Candidate(kind="dash", url=url, content_type=ct, size_hint=size,
                         request_headers=hdrs, notes="dash manifest")

    # YouTube UMP
    if UMP_PATH_RE.search(url) or UMP_MIME_RE.search(ct):
        return Candidate(kind="ump", url=url, content_type=ct, size_hint=size,
                         request_headers=hdrs, notes="youtube videoplayback")

    # Progressive media: prefer the one with the largest known size
    if MEDIA_EXT_RE.search(url_path) or PROGRESSIVE_MIME_RE.match(ct):
        # Drop Vimeo byte-range slices — the Vimeo transport assembles them from playlist.json.
        if VIMEO_SEGMENT_RE.search(url):
            return None
        # Drop range/partial responses — they're HLS/DASH segments, not standalone files.
        if is_range:
            return None
        # Drop tiny responses (< 256KB) that are almost certainly UI audio / thumbnails.
        if size > 0 and size < 256 * 1024:
            return None
        return Candidate(kind="progressive", url=url, content_type=ct, size_hint=size,
                         request_headers=hdrs, notes=f"progressive {ct or '?'}")

    return None
