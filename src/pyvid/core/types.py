from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
from typing import Literal


TransportKind = Literal["progressive", "hls", "dash", "ump", "vimeo", "unknown"]


class TrackKind(str, Enum):
    VIDEO = "video"
    AUDIO = "audio"
    SUBTITLE = "subtitle"
    MUXED = "muxed"


@dataclass
class CapturedRequest:
    url: str
    method: str
    status: int | None
    content_type: str
    size: int
    resource_type: str
    headers: dict[str, str] = field(default_factory=dict)
    request_headers: dict[str, str] = field(default_factory=dict)


@dataclass
class Candidate:
    """A potential downloadable stream found in captured traffic."""
    kind: TransportKind
    url: str                         # manifest URL (HLS/DASH) or media URL (progressive)
    content_type: str = ""
    size_hint: int = 0               # bytes if known
    request_headers: dict[str, str] = field(default_factory=dict)
    notes: str = ""                  # free-form for debugging

    def score(self) -> int:
        """Rough ranking — bigger is better. Orchestrator can override.

        Manifest-based transports always outrank progressive: when a site
        exposes both (e.g. HLS fMP4 where the `main.mp4` byte-range file also
        shows up in traffic), the manifest is the correct entry point.
        """
        if self.kind == "progressive":
            return 100 + self.size_hint // 1024
        base = {"hls": 10_000_000, "dash": 10_000_000, "vimeo": 10_000_100, "ump": 10_000_200}
        return base.get(self.kind, 0) + self.size_hint // 1024


@dataclass
class Track:
    kind: TrackKind
    url: str                         # for manifest-based streams this is the rendition URL
    codec: str = ""
    bandwidth: int = 0               # bits/sec as advertised by manifest
    width: int = 0
    height: int = 0
    language: str = ""
    segments: list[Segment] = field(default_factory=list)
    init_segment: Segment | None = None
    duration: float = 0.0


@dataclass
class Segment:
    url: str
    byte_range: tuple[int, int] | None = None   # (start, end) inclusive, HTTP Range semantics
    duration: float = 0.0


@dataclass
class DownloadResult:
    output_path: str
    tracks: list[Track]
    candidate: Candidate
