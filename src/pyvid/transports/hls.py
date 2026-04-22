"""HLS (.m3u8) transport.

Two-stage flow:
  1. Fetch master playlist, pick highest-bandwidth video rendition (+ default audio if separate).
  2. Fetch media playlists, download all segments with the browser's session cookies,
     concat, and remux to MP4.

Handles:
  - master vs. media playlist (both are .m3u8)
  - EXT-X-MAP initialization segments
  - EXT-X-BYTERANGE sub-ranges
  - EXT-X-KEY AES-128 (delegated to ffmpeg via re-mux step if the key is fetchable)

Does NOT handle:
  - DRM (SAMPLE-AES with Widevine/FairPlay key servers)
  - Live streams (EVENT/LIVE) — treated as VOD truncated to current window.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from urllib.parse import urljoin

import httpx
import m3u8
from playwright.async_api import BrowserContext

from ..core.mux import concat_files, remux
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
        master_txt = (await client.get(url)).text
        master = m3u8.loads(master_txt, uri=url)

        video_pl_url, audio_pl_url = _pick_renditions(master, url)
        print(f"[hls] video playlist: {video_pl_url}", file=sys.stderr)
        if audio_pl_url:
            print(f"[hls] audio playlist: {audio_pl_url}", file=sys.stderr)

        video_file = await _download_playlist(client, video_pl_url, output_dir, "video")
        audio_file = None
        if audio_pl_url:
            audio_file = await _download_playlist(client, audio_pl_url, output_dir, "audio")

    # Remux to mp4. If audio is a separate track, mux it in.
    out = output_dir / "output.mp4"
    if audio_file:
        from ..core.mux import mux_tracks
        mux_tracks(video_file, audio_file, out)
    else:
        remux(video_file, out)
    video_file.unlink(missing_ok=True)
    if audio_file:
        audio_file.unlink(missing_ok=True)

    print(f"[hls] done -> {out}", file=sys.stderr)
    return DownloadResult(
        output_path=str(out),
        tracks=[Track(kind=TrackKind.VIDEO, url=video_pl_url)]
        + ([Track(kind=TrackKind.AUDIO, url=audio_pl_url)] if audio_pl_url else []),
        candidate=candidate,
    )


def _pick_renditions(master: m3u8.M3U8, master_url: str) -> tuple[str, str | None]:
    """Return (video_media_playlist_url, audio_media_playlist_url_or_None).

    If `master` is already a media playlist, returns its own URL for video and None.
    """
    if not master.is_variant:
        return master_url, None

    best = max(master.playlists, key=lambda p: p.stream_info.bandwidth or 0)
    video_url = best.absolute_uri

    audio_url: str | None = None
    if best.stream_info.audio:
        group_id = best.stream_info.audio
        for media in master.media:
            if media.type == "AUDIO" and media.group_id == group_id and media.uri:
                audio_url = urljoin(master_url, media.uri)
                if media.default == "YES":
                    break
    return video_url, audio_url


async def _download_playlist(
    client: httpx.AsyncClient,
    playlist_url: str,
    output_dir: Path,
    label: str,
) -> Path:
    pl_txt = (await client.get(playlist_url)).text
    pl = m3u8.loads(pl_txt, uri=playlist_url)

    if pl.keys and any(k for k in pl.keys if k and k.method and k.method.upper() not in ("NONE", "AES-128")):
        raise RuntimeError(
            f"unsupported HLS encryption method(s): {[k.method for k in pl.keys if k]}. "
            "Only AES-128 with fetchable key is supported."
        )
    # AES-128 handled post-hoc: we concat raw, then let ffmpeg decrypt. For simplicity now,
    # if encryption is present we pass the original m3u8 to ffmpeg instead of manual concat.
    if any(k and k.method and k.method.upper() == "AES-128" for k in pl.keys):
        print(f"[hls] {label}: AES-128 encrypted, delegating to ffmpeg", file=sys.stderr)
        out = output_dir / f"{label}.ts"
        # Write playlist to a local file so ffmpeg uses our session?
        # Simpler: pass headers via -headers. But ffmpeg's -headers doesn't cover cookies well.
        # For v0, just point ffmpeg at the playlist URL directly.
        import subprocess
        from ..core.mux import ensure_ffmpeg
        ff = ensure_ffmpeg()
        subprocess.run(
            [ff, "-hide_banner", "-loglevel", "error", "-y",
             "-i", playlist_url, "-c", "copy", str(out)],
            check=True,
        )
        return out

    segs = pl.segments
    tasks: list[asyncio.Task] = []
    part_paths: list[Path] = []

    init_uri: str | None = None
    if segs and segs[0].init_section and segs[0].init_section.uri:
        init_uri = urljoin(playlist_url, segs[0].init_section.uri)

    sem = asyncio.Semaphore(8)

    async def fetch(i: int, seg_url: str, byterange: str | None) -> Path:
        async with sem:
            extra = {}
            if byterange:
                # m3u8 byterange is "length[@offset]"
                parts = byterange.split("@")
                length = int(parts[0])
                offset = int(parts[1]) if len(parts) > 1 else 0
                extra["Range"] = f"bytes={offset}-{offset + length - 1}"
            r = await client.get(seg_url, headers=extra or None)
            r.raise_for_status()
            p = output_dir / f"{label}_{i:06d}.seg"
            p.write_bytes(r.content)
            _progress(label, i + 1, len(segs))
            return p

    if init_uri:
        init_r = await client.get(init_uri)
        init_r.raise_for_status()
        init_path = output_dir / f"{label}_init.seg"
        init_path.write_bytes(init_r.content)
        part_paths.append(init_path)

    for i, s in enumerate(segs):
        seg_url = urljoin(playlist_url, s.uri)
        byterange = s.byterange if s.byterange else None
        tasks.append(asyncio.create_task(fetch(i, seg_url, byterange)))

    fetched = await asyncio.gather(*tasks)
    sys.stderr.write("\n")
    part_paths.extend(fetched)

    # Concat. For fMP4 (.m4s) segments, binary concat is safe. For MPEG-TS, also safe.
    merged = output_dir / f"{label}.concat"
    concat_files(part_paths, merged, mode="binary")

    for p in part_paths:
        p.unlink(missing_ok=True)
    return merged


def _progress(label: str, done: int, total: int) -> None:
    pct = done / total * 100 if total else 100
    bar_len = 30
    filled = int(bar_len * done / total) if total else bar_len
    bar = "#" * filled + "-" * (bar_len - filled)
    sys.stderr.write(f"\r[hls] {label} [{bar}] {done}/{total} {pct:5.1f}%")
    sys.stderr.flush()
