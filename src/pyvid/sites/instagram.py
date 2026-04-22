"""Instagram site module — strip byte-range params from fbcdn URLs.

Instagram's web player fetches reel/post MP4s from `*.fbcdn.net` using
partial-range requests baked into the URL:

    .../video.mp4?bytestart=972&byteend=295246&<other params>

The same underlying URL serves the full file if you omit those two params.
Our sniffer sees several candidates (one per range fetch); we pick the
first one, strip the range params, and hand off to the progressive
transport.

Scope: works for *public* reels/posts that don't require login. Private
or login-gated content will not produce candidates in the first place
because the page won't render the video for an anonymous session.
"""
from __future__ import annotations

import sys
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from ..core.session import SessionState
from ..core.types import Candidate


FBCDN_HOSTS = ("fbcdn.net", "cdninstagram.com")


def choose_candidate(candidates: list[Candidate], _session: SessionState) -> Candidate | None:
    for c in candidates:
        host = urlparse(c.url).hostname or ""
        if not any(host.endswith(h) for h in FBCDN_HOSTS):
            continue
        if ".mp4" not in urlparse(c.url).path.lower():
            continue
        clean_url = _strip_range_params(c.url)
        print(f"[instagram] picked fbcdn MP4, stripped byte-range params", file=sys.stderr)
        return Candidate(
            kind="progressive",
            url=clean_url,
            content_type="video/mp4",
            size_hint=0,
            request_headers={"Referer": "https://www.instagram.com/"},
            notes="instagram fbcdn progressive (ranges stripped)",
        )
    return None


def _strip_range_params(url: str) -> str:
    parts = urlparse(url)
    q = parse_qs(parts.query, keep_blank_values=True)
    q.pop("bytestart", None)
    q.pop("byteend", None)
    flat = [(k, v) for k, vs in q.items() for v in vs]
    return urlunparse(parts._replace(query=urlencode(flat)))
