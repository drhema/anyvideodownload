"""YouTube site module — browser-intercept UMP reassembly.

Strategy
--------
YouTube's web player does the hard cryptographic work for us: PoToken,
`n`-param deobfuscation, signature cipher, SABR request construction.
We just let Chromium play the video, capture every `videoplayback`
response body, demux the onesie-framed ONESIE_DATA chunks by stream_id,
and mux the resulting WebM streams into an MP4.

Flow:
  1. Open YouTube URL in Playwright (session records all responses)
  2. Hook on_response to capture bodies for googlevideo/videoplayback
  3. Wait for initial buffer to fill
  4. Seek-scan through the video (8 points) to force full-range buffering
  5. Demux captured bodies by stream_id using ump.demux_onesie
  6. Probe each stream with ffprobe to identify video vs audio
  7. Pick highest-size video + highest-size audio
  8. Mux via ffmpeg → output.mp4

Known limits (accept these for v0, document, iterate):
  - Picks whatever quality the browser's ABR selected — not explicit selection
  - Ads before the video pollute the capture (filter TBD)
  - PoToken-restricted streams fall back to degraded quality
  - Stream-id mapping is session-specific; we use size heuristics to pick
"""
from __future__ import annotations

import asyncio
import shutil
import subprocess
import sys
from pathlib import Path

from ..core.mux import mux_tracks, remux
from ..core.session import open_session
from ..core.types import Candidate, DownloadResult, Track, TrackKind
from ..transports.ump import demux_onesie


async def download_page(
    url: str,
    output_dir: Path,
    *,
    headless: bool = False,
    idle_ms: int = 4000,
    max_ms: int = 240000,
    format_override: str | None = None,
    **_,
) -> DownloadResult:
    output_dir = Path(output_dir)
    bodies: list[bytes] = []
    vp_response_count = {"n": 0}

    async def on_response(res, _cap) -> None:
        if "googlevideo.com/videoplayback" not in res.url:
            return
        vp_response_count["n"] += 1
        try:
            body = await res.body()
        except Exception as e:
            print(f"[youtube] body() failed for response {vp_response_count['n']}: {e}", file=sys.stderr)
            return
        if body:
            bodies.append(body)
            print(f"[youtube] captured body #{len(bodies)} ({len(body)} bytes)", file=sys.stderr)

    async with open_session(
        url,
        headless=headless,
        idle_ms=idle_ms,
        max_ms=max_ms,
        on_response=on_response,
    ) as session:
        # Two-pass strategy:
        #   1. Seek to near-end (95%) and wait — forces YouTube to fetch the
        #      tail clusters (otherwise we never see them if the video is long).
        #   2. Seek back to 0 and play at 2x to finish — delivers clusters in
        #      timestamp order. End-buffer from pass 1 may be redundantly
        #      re-fetched, but that's fine; ffmpeg remux-pass dedupes.
        # We avoid interleaved multi-point seeks because they produce
        # out-of-order WebM clusters that aren't safely reassemblable.
        try:
            play_result = await session.page.evaluate(
                r"""async () => {
                    const v = document.querySelector('video');
                    if (!v) return { ok: false, reason: 'no <video>' };
                    const start = Date.now();
                    while (!isFinite(v.duration) && Date.now() - start < 10000) {
                        await new Promise(r => setTimeout(r, 300));
                    }
                    const dur = v.duration;
                    if (!isFinite(dur) || !dur) return { ok: false, reason: 'no duration' };
                    v.muted = true;

                    // Pass 1: seek near the end to force tail buffering.
                    if (dur > 10) {
                        v.currentTime = Math.max(dur - 3, dur * 0.95);
                        try { await v.play(); } catch(e) {}
                        // Give YouTube time to request the tail clusters.
                        await new Promise(r => setTimeout(r, 6000));
                        try { v.pause(); } catch(e) {}
                    }

                    // Pass 2: rewind and play the whole thing at 2x.
                    v.currentTime = 0;
                    v.playbackRate = 2.0;
                    try { await v.play(); } catch(e) {}
                    const maxWait = Math.min((dur / 2 + 15) * 1000, 240000);
                    const t0 = Date.now();
                    while (!v.ended && Date.now() - t0 < maxWait) {
                        await new Promise(r => setTimeout(r, 500));
                    }
                    return { ok: true, duration: dur, reached: v.currentTime, ended: v.ended };
                }"""
            )
            print(f"[youtube] playback: {play_result}", file=sys.stderr)
        except Exception as e:
            print(f"[youtube] playback hook failed (continuing): {e}", file=sys.stderr)

        # Drain tail: wait a few seconds for any final responses.
        await asyncio.sleep(5)

    print(f"[youtube] captured {len(bodies)} UMP bodies (saw {vp_response_count['n']} videoplayback responses)", file=sys.stderr)
    if not bodies:
        raise RuntimeError(
            "no UMP responses captured — YouTube may have blocked playback "
            "(check for age-gate, region block, or sign-in wall in the browser)"
        )

    streams = demux_onesie(bodies)
    print(f"[youtube] demuxed into {len(streams)} stream(s):", file=sys.stderr)
    for sid, data in sorted(streams.items()):
        print(f"    stream 0x{sid:02x}: {len(data):>10} bytes", file=sys.stderr)

    output_dir.mkdir(parents=True, exist_ok=True)
    tmpdir = output_dir / "_ytbuf"
    tmpdir.mkdir(exist_ok=True)

    # YouTube multiplexes multiple streams per response. Different stream_ids
    # can be (a) the init segment of a rendition and (b) a succession of
    # moof+mdat fragments that build on it. We can't rely on stream_id parity;
    # instead we classify each by its container-format signature and
    # concatenate same-format streams in stream_id order.
    groups: list[tuple[str, list[int], Path]] = []
    by_format: dict[str, list[tuple[int, bytes]]] = {"fmp4": [], "webm": [], "other": []}
    for sid, data in streams.items():
        by_format[_classify_container(data)].append((sid, data))

    for fmt, items in by_format.items():
        if not items or fmt == "other":
            continue
        items.sort(key=lambda x: x[0])
        sid_list = [sid for sid, _ in items]
        label = f"{fmt}_" + "_".join(f"{sid:02x}" for sid in sid_list)
        ext = "mp4" if fmt == "fmp4" else "webm"
        p = tmpdir / f"group_{label}.{ext}"
        with p.open("wb") as f:
            for _, data in items:
                f.write(data)
        groups.append((label, sid_list, p))

    probed: list[dict] = []
    for label, group_ids, path in groups:
        info = _probe(path)
        size = path.stat().st_size
        if not info["codec_type"]:
            # Sometimes clusters land out of order (seek-scan side effect). Try a
            # ffmpeg remux which reorders by timestamp.
            fixed = path.with_suffix(".fixed.webm")
            try:
                subprocess.run(
                    ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                     "-i", str(path), "-c", "copy", str(fixed)],
                    check=False, capture_output=True, timeout=60,
                )
                if fixed.exists() and fixed.stat().st_size > 0:
                    info = _probe(fixed)
                    if info["codec_type"]:
                        path = fixed
            except Exception:
                pass
        if info["codec_type"]:
            probed.append(dict(**info, label=label, group_ids=group_ids, path=path, size=path.stat().st_size))
            print(
                f"    group {label}: {info['codec_type']} ({info['codec_name']}) "
                f"{info.get('width', 0)}x{info.get('height', 0)} {path.stat().st_size} bytes",
                file=sys.stderr,
            )
        else:
            print(f"    group {label}: unrecognized ({size} bytes) — kept at {path}", file=sys.stderr)

    videos = [p for p in probed if p["codec_type"] == "video"]
    audios = [p for p in probed if p["codec_type"] == "audio"]
    if not videos:
        raise RuntimeError(
            f"no video stream identified among {len(probed)} probed groups. "
            "Captured bodies may be incomplete — try --max-ms 300000 for longer videos."
        )
    video = max(videos, key=lambda p: (p.get("width", 0) * p.get("height", 0), p["size"]))
    audio = max(audios, key=lambda p: p["size"]) if audios else None

    print(
        f"[youtube] chose video group {video['label']} "
        f"({video['codec_name']} {video.get('width', '?')}x{video.get('height', '?')})"
        + (f" + audio group {audio['label']} ({audio['codec_name']})" if audio else ""),
        file=sys.stderr,
    )

    out = output_dir / "output.mp4"
    if audio:
        mux_tracks(video["path"], audio["path"], out)
    else:
        remux(video["path"], out)

    shutil.rmtree(tmpdir, ignore_errors=True)
    print(f"[youtube] done -> {out}", file=sys.stderr)

    tracks: list[Track] = [Track(kind=TrackKind.VIDEO, url=url, codec=video["codec_name"],
                                 width=video.get("width", 0), height=video.get("height", 0))]
    if audio:
        tracks.append(Track(kind=TrackKind.AUDIO, url=url, codec=audio["codec_name"]))

    return DownloadResult(
        output_path=str(out),
        tracks=tracks,
        candidate=Candidate(kind="ump", url=url, content_type="application/vnd.yt-ump",
                            notes="youtube ump browser-intercept"),
    )


def _classify_container(data: bytes) -> str:
    """Return 'fmp4', 'webm', or 'other' based on first-bytes signature.

    fMP4 payloads start with a 4-byte box-size followed by an ASCII 4CC:
    'ftyp' (init segment) or 'moof' (fragment).
    WebM payloads start with the EBML header 1a 45 df a3, or — for
    continuation chunks — a cluster element (1f 43 b6 75).
    """
    if len(data) < 8:
        return "other"
    if data[4:8] in (b"ftyp", b"moof", b"moov", b"styp", b"sidx", b"mdat"):
        return "fmp4"
    if data[:4] == b"\x1a\x45\xdf\xa3" or data[:4] == b"\x1f\x43\xb6\x75":
        return "webm"
    return "other"


def _probe(path: Path) -> dict:
    """Return dict with keys codec_type, codec_name, width, height (0 if unknown)."""
    import json as _json
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(path)],
            capture_output=True, text=True, timeout=15,
        )
        if r.returncode != 0:
            return {"codec_type": "", "codec_name": "", "width": 0, "height": 0}
        data = _json.loads(r.stdout or "{}")
        streams = data.get("streams") or []
        # Prefer video stream; fall back to first audio
        for s in streams:
            if s.get("codec_type") == "video":
                return {
                    "codec_type": "video",
                    "codec_name": s.get("codec_name", ""),
                    "width": int(s.get("width") or 0),
                    "height": int(s.get("height") or 0),
                }
        for s in streams:
            if s.get("codec_type") == "audio":
                return {
                    "codec_type": "audio",
                    "codec_name": s.get("codec_name", ""),
                    "width": 0,
                    "height": 0,
                }
    except Exception:
        pass
    return {"codec_type": "", "codec_name": "", "width": 0, "height": 0}


# Legacy hook kept for compatibility — returns None so the standard flow would
# error out, but orchestrator now prefers download_page above.
def choose_candidate(_candidates, _session):
    return None
