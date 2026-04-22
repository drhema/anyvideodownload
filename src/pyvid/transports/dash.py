"""DASH (.mpd) transport.

Parses MPEG-DASH manifest, picks highest-bandwidth video + default/first audio
Representation, resolves segment URLs from SegmentTemplate/SegmentList/SegmentBase,
downloads in parallel with browser cookies, then muxes to MP4.

Supported:
  - SegmentTemplate with $Number$ + @duration + @startNumber (+ MPD@mediaPresentationDuration)
  - SegmentTemplate with $Number$ + SegmentTimeline
  - SegmentTemplate with $Time$ + SegmentTimeline
  - SegmentBase with Initialization range (single-file DASH)
  - SegmentList
  - $RepresentationID$ / $Bandwidth$ / $Number%0Nd$ substitution
  - Absolute or relative BaseURL at MPD/Period/AdaptationSet/Representation level

Not yet supported (raises a clear error):
  - Multi-Period manifests (only first Period used)
  - ContentProtection / Widevine / PlayReady (out of scope — DRM)
  - Xlink / external periods
  - Live (dynamic) manifests — VOD only for v0
"""
from __future__ import annotations

import asyncio
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urljoin
from xml.etree import ElementTree as ET

import httpx
from playwright.async_api import BrowserContext

from ..core.mux import concat_files, mux_tracks, remux
from ..core.types import Candidate, DownloadResult, Track, TrackKind
from .base import session_headers


NS = {"mpd": "urn:mpeg:dash:schema:mpd:2011"}


@dataclass
class Representation:
    id: str
    bandwidth: int
    mime_type: str
    codecs: str
    width: int = 0
    height: int = 0
    base_url: str = ""
    init_url: str | None = None
    init_range: tuple[int, int] | None = None
    media_urls: list[str] = field(default_factory=list)
    byte_ranges: list[tuple[int, int] | None] = field(default_factory=list)


@dataclass
class MpdContext:
    mpd_url: str
    period_seconds: float | None


async def download(
    candidate: Candidate,
    ctx: BrowserContext,
    output_dir: Path,
) -> DownloadResult:
    url = candidate.url
    headers = await session_headers(ctx, url, candidate.request_headers)
    output_dir.mkdir(parents=True, exist_ok=True)

    async with httpx.AsyncClient(http2=True, headers=headers, follow_redirects=True, timeout=60) as client:
        mpd_text = (await client.get(url)).text
        if "ContentProtection" in mpd_text:
            raise RuntimeError(
                "DRM (ContentProtection) detected in MPD — out of scope. "
                "This tool does not bypass Widevine/PlayReady/FairPlay."
            )
        root = ET.fromstring(mpd_text)
        if root.get("type") == "dynamic":
            raise RuntimeError("dynamic (live) DASH not yet supported — v0 is VOD only.")

        video_rep, audio_rep = _pick_representations(root, url)

        video_file = await _download_representation(client, video_rep, output_dir, "video")
        audio_file = None
        if audio_rep:
            audio_file = await _download_representation(client, audio_rep, output_dir, "audio")

    out = output_dir / "output.mp4"
    if audio_file:
        mux_tracks(video_file, audio_file, out)
    else:
        remux(video_file, out)
    video_file.unlink(missing_ok=True)
    if audio_file:
        audio_file.unlink(missing_ok=True)
    print(f"[dash] done -> {out}", file=sys.stderr)

    tracks = [Track(kind=TrackKind.VIDEO, url=video_rep.base_url,
                    bandwidth=video_rep.bandwidth, width=video_rep.width, height=video_rep.height,
                    codec=video_rep.codecs)]
    if audio_rep:
        tracks.append(Track(kind=TrackKind.AUDIO, url=audio_rep.base_url,
                            bandwidth=audio_rep.bandwidth, codec=audio_rep.codecs))
    return DownloadResult(output_path=str(out), tracks=tracks, candidate=candidate)


def _pick_representations(root: ET.Element, mpd_url: str) -> tuple[Representation, Representation | None]:
    periods = root.findall("mpd:Period", NS)
    if not periods:
        raise RuntimeError("no <Period> in MPD")
    period = periods[0]

    period_seconds = _parse_iso8601_duration(period.get("duration")) \
        or _parse_iso8601_duration(root.get("mediaPresentationDuration"))

    mpd_ctx = MpdContext(mpd_url=mpd_url, period_seconds=period_seconds)
    mpd_base = _base_url(root, mpd_url)
    period_base = _base_url(period, mpd_base)

    video_candidates: list[Representation] = []
    audio_candidates: list[Representation] = []

    for adset in period.findall("mpd:AdaptationSet", NS):
        adset_base = _base_url(adset, period_base)
        content_type = (adset.get("contentType") or adset.get("mimeType") or "").lower()
        bucket: list[Representation] | None
        if "video" in content_type:
            bucket = video_candidates
        elif "audio" in content_type:
            bucket = audio_candidates
        else:
            first_rep = adset.find("mpd:Representation", NS)
            if first_rep is None:
                continue
            mt = (first_rep.get("mimeType") or "").lower()
            if "video" in mt:
                bucket = video_candidates
            elif "audio" in mt:
                bucket = audio_candidates
            else:
                continue

        for rep_el in adset.findall("mpd:Representation", NS):
            rep_base = _base_url(rep_el, adset_base)
            rep = _build_representation(rep_el, adset, rep_base, mpd_ctx)
            if rep is not None:
                bucket.append(rep)

    if not video_candidates:
        raise RuntimeError("no usable video representations in MPD")
    video_rep = max(video_candidates, key=lambda r: r.bandwidth)
    audio_rep = max(audio_candidates, key=lambda r: r.bandwidth) if audio_candidates else None
    return video_rep, audio_rep


def _base_url(el: ET.Element, parent_base: str) -> str:
    bu = el.find("mpd:BaseURL", NS)
    if bu is not None and bu.text:
        return urljoin(parent_base, bu.text.strip())
    return parent_base


def _build_representation(rep_el: ET.Element, adset: ET.Element, base_url: str,
                          mpd_ctx: MpdContext) -> Representation | None:
    rep = Representation(
        id=rep_el.get("id", ""),
        bandwidth=int(rep_el.get("bandwidth", "0") or 0),
        mime_type=rep_el.get("mimeType") or adset.get("mimeType", ""),
        codecs=rep_el.get("codecs") or adset.get("codecs", ""),
        width=int(rep_el.get("width", "0") or 0),
        height=int(rep_el.get("height", "0") or 0),
        base_url=base_url,
    )

    st = rep_el.find("mpd:SegmentTemplate", NS) or adset.find("mpd:SegmentTemplate", NS)
    if st is not None:
        return _apply_segment_template(rep, st, base_url, mpd_ctx)

    sl = rep_el.find("mpd:SegmentList", NS) or adset.find("mpd:SegmentList", NS)
    if sl is not None:
        return _apply_segment_list(rep, sl, base_url)

    sb = rep_el.find("mpd:SegmentBase", NS) or adset.find("mpd:SegmentBase", NS)
    if sb is not None:
        return _apply_segment_base(rep, sb, base_url)

    rep.media_urls = [base_url]
    rep.byte_ranges = [None]
    return rep


def _apply_segment_template(rep: Representation, st: ET.Element, base_url: str,
                             mpd_ctx: MpdContext) -> Representation:
    media_tpl = st.get("media", "")
    init_tpl = st.get("initialization", "")
    timescale = int(st.get("timescale", "1") or 1)
    start_number = int(st.get("startNumber", "1") or 1)
    duration_attr = st.get("duration")

    def sub(tpl: str, *, number: int | None = None, time: int | None = None) -> str:
        def repl(m: re.Match[str]) -> str:
            token = m.group(1)
            if token == "RepresentationID":
                return rep.id
            if token == "Bandwidth":
                return str(rep.bandwidth)
            if token == "Number" and number is not None:
                return str(number)
            if token.startswith("Number%") and number is not None:
                fmt = "%" + token[len("Number"):]
                return fmt % number
            if token == "Time" and time is not None:
                return str(time)
            return m.group(0)
        return re.sub(r"\$([A-Za-z0-9%0-9d]+?)\$", repl, tpl)

    if init_tpl:
        rep.init_url = urljoin(base_url, sub(init_tpl))

    timeline = st.find("mpd:SegmentTimeline", NS)
    if timeline is not None:
        t_cursor = 0
        number = start_number
        for s_el in timeline.findall("mpd:S", NS):
            t_attr = s_el.get("t")
            if t_attr is not None:
                t_cursor = int(t_attr)
            d = int(s_el.get("d"))
            r = int(s_el.get("r", "0") or 0)
            count = r + 1
            for _ in range(count):
                if "$Time$" in media_tpl:
                    rep.media_urls.append(urljoin(base_url, sub(media_tpl, time=t_cursor)))
                else:
                    rep.media_urls.append(urljoin(base_url, sub(media_tpl, number=number)))
                rep.byte_ranges.append(None)
                t_cursor += d
                number += 1
        return rep

    if duration_attr:
        if mpd_ctx.period_seconds is None:
            raise RuntimeError(
                "SegmentTemplate has @duration but MPD has no mediaPresentationDuration "
                "or Period@duration — cannot compute segment count."
            )
        seg_dur = int(duration_attr)
        total = int((mpd_ctx.period_seconds * timescale + seg_dur - 1) // seg_dur)
        for i in range(total):
            n = start_number + i
            rep.media_urls.append(urljoin(base_url, sub(media_tpl, number=n)))
            rep.byte_ranges.append(None)
        return rep

    raise RuntimeError("SegmentTemplate without SegmentTimeline or @duration — unsupported")


def _apply_segment_list(rep: Representation, sl: ET.Element, base_url: str) -> Representation:
    init_el = sl.find("mpd:Initialization", NS)
    if init_el is not None:
        source = init_el.get("sourceURL")
        rng = init_el.get("range")
        rep.init_url = urljoin(base_url, source) if source else base_url
        if rng:
            a, b = rng.split("-")
            rep.init_range = (int(a), int(b))
    for s in sl.findall("mpd:SegmentURL", NS):
        media = s.get("media")
        rng = s.get("mediaRange")
        rep.media_urls.append(urljoin(base_url, media) if media else base_url)
        if rng:
            a, b = rng.split("-")
            rep.byte_ranges.append((int(a), int(b)))
        else:
            rep.byte_ranges.append(None)
    return rep


def _apply_segment_base(rep: Representation, sb: ET.Element, base_url: str) -> Representation:
    init_el = sb.find("mpd:Initialization", NS)
    if init_el is not None:
        rng = init_el.get("range")
        if rng:
            a, b = rng.split("-")
            rep.init_range = (int(a), int(b))
        rep.init_url = base_url
    rep.media_urls = [base_url]
    rep.byte_ranges = [None]
    return rep


_ISO8601_DURATION_RE = re.compile(
    r"^P(?:(?P<days>\d+(?:\.\d+)?)D)?"
    r"(?:T(?:(?P<hours>\d+(?:\.\d+)?)H)?(?:(?P<minutes>\d+(?:\.\d+)?)M)?(?:(?P<seconds>\d+(?:\.\d+)?)S)?)?$"
)


def _parse_iso8601_duration(s: str | None) -> float | None:
    if not s:
        return None
    m = _ISO8601_DURATION_RE.match(s)
    if not m:
        return None
    d = float(m.group("days") or 0)
    h = float(m.group("hours") or 0)
    mi = float(m.group("minutes") or 0)
    sec = float(m.group("seconds") or 0)
    return d * 86400 + h * 3600 + mi * 60 + sec


async def _download_representation(
    client: httpx.AsyncClient,
    rep: Representation,
    output_dir: Path,
    label: str,
) -> Path:
    part_paths: list[Path] = []
    sem = asyncio.Semaphore(8)
    total_items = len(rep.media_urls) + (1 if rep.init_url else 0)

    async def fetch(i: int, url: str, byterange: tuple[int, int] | None, tag: str) -> Path:
        async with sem:
            extra: dict[str, str] = {}
            if byterange:
                a, b = byterange
                extra["Range"] = f"bytes={a}-{b}"
            r = await client.get(url, headers=extra or None)
            r.raise_for_status()
            p = output_dir / f"{label}_{tag}_{i:06d}.seg"
            p.write_bytes(r.content)
            _progress(label, i + 1, total_items)
            return p

    next_index = 0
    if rep.init_url:
        init_p = await fetch(next_index, rep.init_url, rep.init_range, "init")
        part_paths.append(init_p)
        next_index += 1

    tasks = []
    for i, (u, br) in enumerate(zip(rep.media_urls, rep.byte_ranges)):
        tasks.append(asyncio.create_task(fetch(next_index + i, u, br, "seg")))
    fetched = await asyncio.gather(*tasks)
    sys.stderr.write("\n")
    part_paths.extend(fetched)

    merged = output_dir / f"{label}.concat.mp4"
    concat_files(part_paths, merged, mode="binary")
    for p in part_paths:
        p.unlink(missing_ok=True)
    return merged


def _progress(label: str, done: int, total: int) -> None:
    pct = done / total * 100 if total else 100
    bar_len = 30
    filled = int(bar_len * done / total) if total else bar_len
    bar = "#" * filled + "-" * (bar_len - filled)
    sys.stderr.write(f"\r[dash] {label} [{bar}] {done}/{total} {pct:5.1f}%")
    sys.stderr.flush()
