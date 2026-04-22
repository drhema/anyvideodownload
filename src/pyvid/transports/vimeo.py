"""Vimeo adaptive (DASH-in-JSON) transport.

Vimeo's player doesn't use standard DASH/HLS. It fetches a `playlist.json`
from `vod-adaptive*.vimeocdn.com` which describes video + audio renditions:

    {
      "base_url": "../../../../../range/prot/",
      "video": [
        {
          "id": "...", "bitrate": ..., "width": ..., "height": ...,
          "init_segment": "<base64 ftyp+moov bytes>",
          "segments": [{"url": "<rel>", "start": 0, "end": 6, "size": N}, ...]
        },
        ...
      ],
      "audio": [ ... same shape ... ]
    }

Each segment URL is relative and points to a byte-range slice of a single MP4.
The init_segment is base64-embedded directly in the JSON.
"""
from __future__ import annotations

import asyncio
import base64
import sys
from pathlib import Path
from urllib.parse import urljoin

import httpx
from playwright.async_api import BrowserContext

from ..core.mux import concat_files, mux_tracks, remux
from ..core.types import Candidate, DownloadResult, Track, TrackKind
from .base import session_headers


async def download(
    candidate: Candidate,
    ctx: BrowserContext,
    output_dir: Path,
) -> DownloadResult:
    url = candidate.url
    headers = await session_headers(ctx, url, candidate.request_headers)
    output_dir.mkdir(parents=True, exist_ok=True)

    async with httpx.AsyncClient(http2=True, headers=headers, follow_redirects=True, timeout=60) as client:
        r = await client.get(url)
        r.raise_for_status()
        manifest = r.json()

        base_url_from_playlist = manifest.get("base_url", "")
        # Resolve the playlist's own base URL (needed for urljoin with relative segment URLs)
        manifest_base = urljoin(url, base_url_from_playlist)

        video_rend = _pick_best(manifest.get("video", []))
        audio_rend = _pick_best(manifest.get("audio", []))
        if video_rend is None:
            raise RuntimeError("vimeo playlist.json has no video renditions")

        print(f"[vimeo] video: {video_rend.get('width')}x{video_rend.get('height')} "
              f"@ {video_rend.get('bitrate', 0) // 1000}kbps "
              f"({len(video_rend.get('segments', []))} segments)", file=sys.stderr)
        if audio_rend:
            print(f"[vimeo] audio: {audio_rend.get('bitrate', 0) // 1000}kbps "
                  f"({len(audio_rend.get('segments', []))} segments)", file=sys.stderr)

        video_file = await _download_rendition(client, video_rend, manifest_base, output_dir, "video")
        audio_file = None
        if audio_rend:
            audio_file = await _download_rendition(client, audio_rend, manifest_base, output_dir, "audio")

    out = output_dir / "output.mp4"
    if audio_file:
        mux_tracks(video_file, audio_file, out)
    else:
        remux(video_file, out)
    video_file.unlink(missing_ok=True)
    if audio_file:
        audio_file.unlink(missing_ok=True)
    print(f"[vimeo] done -> {out}", file=sys.stderr)

    tracks = [Track(
        kind=TrackKind.VIDEO, url=url, codec=video_rend.get("codecs", ""),
        bandwidth=video_rend.get("bitrate", 0),
        width=video_rend.get("width", 0), height=video_rend.get("height", 0),
        duration=video_rend.get("duration", 0.0),
    )]
    if audio_rend:
        tracks.append(Track(
            kind=TrackKind.AUDIO, url=url, codec=audio_rend.get("codecs", ""),
            bandwidth=audio_rend.get("bitrate", 0),
            duration=audio_rend.get("duration", 0.0),
        ))
    return DownloadResult(output_path=str(out), tracks=tracks, candidate=candidate)


def _pick_best(renditions: list[dict]) -> dict | None:
    if not renditions:
        return None
    return max(renditions, key=lambda r: r.get("bitrate", 0) or r.get("avg_bitrate", 0))


async def _download_rendition(
    client: httpx.AsyncClient,
    rend: dict,
    manifest_base: str,
    output_dir: Path,
    label: str,
) -> Path:
    rendition_base = urljoin(manifest_base, rend.get("base_url", ""))
    segments = rend.get("segments", [])
    total = len(segments) + 1  # +1 for init

    init_bytes = base64.b64decode(rend["init_segment"])
    init_path = output_dir / f"{label}_init.seg"
    init_path.write_bytes(init_bytes)
    _progress(label, 1, total)

    sem = asyncio.Semaphore(8)
    done_count = {"n": 1}

    async def fetch(i: int, seg: dict) -> Path:
        async with sem:
            seg_url = urljoin(rendition_base, seg["url"])
            r = await client.get(seg_url)
            r.raise_for_status()
            p = output_dir / f"{label}_{i:06d}.seg"
            p.write_bytes(r.content)
            done_count["n"] += 1
            _progress(label, done_count["n"], total)
            return p

    tasks = [asyncio.create_task(fetch(i, s)) for i, s in enumerate(segments)]
    seg_paths = await asyncio.gather(*tasks)
    sys.stderr.write("\n")

    all_parts = [init_path] + list(seg_paths)
    merged = output_dir / f"{label}.concat.mp4"
    concat_files(all_parts, merged, mode="binary")
    for p in all_parts:
        p.unlink(missing_ok=True)
    return merged


def _progress(label: str, done: int, total: int) -> None:
    pct = done / total * 100 if total else 100
    bar_len = 30
    filled = int(bar_len * done / total) if total else bar_len
    bar = "#" * filled + "-" * (bar_len - filled)
    sys.stderr.write(f"\r[vimeo] {label} [{bar}] {done}/{total} {pct:5.1f}%")
    sys.stderr.flush()
