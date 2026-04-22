"""Facebook site module — same fbcdn strategy as Instagram.

Facebook serves MP4 video from *.fbcdn.net using the same byte-range
URL pattern as Instagram (both are Meta properties sharing a CDN layer).
The site module logic is identical: find an fbcdn MP4 candidate and
strip `bytestart`/`byteend` to fetch the whole file.

Known limitation: most Facebook videos require authentication to
render. Logged-out sessions typically hit a login wall and the player
never loads, so no candidate appears. Public videos on public pages
*sometimes* play anonymously — if they do, this module works.

To support authenticated Facebook content in the future, we'd need to
persist a logged-in browser profile (--user-data-dir) between runs.
"""
from __future__ import annotations

import sys
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from ..core.session import SessionState
from ..core.types import Candidate


FBCDN_HOSTS = ("fbcdn.net",)


def choose_candidate(candidates: list[Candidate], _session: SessionState) -> Candidate | None:
    for c in candidates:
        host = urlparse(c.url).hostname or ""
        if not any(host.endswith(h) for h in FBCDN_HOSTS):
            continue
        if ".mp4" not in urlparse(c.url).path.lower():
            continue
        clean_url = _strip_range_params(c.url)
        print("[facebook] picked fbcdn MP4, stripped byte-range params", file=sys.stderr)
        return Candidate(
            kind="progressive",
            url=clean_url,
            content_type="video/mp4",
            size_hint=0,
            request_headers={"Referer": "https://www.facebook.com/"},
            notes="facebook fbcdn progressive (ranges stripped)",
        )
    return None


def _strip_range_params(url: str) -> str:
    parts = urlparse(url)
    q = parse_qs(parts.query, keep_blank_values=True)
    q.pop("bytestart", None)
    q.pop("byteend", None)
    flat = [(k, v) for k, vs in q.items() for v in vs]
    return urlunparse(parts._replace(query=urlencode(flat)))
