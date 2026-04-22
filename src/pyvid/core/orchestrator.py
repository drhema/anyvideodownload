"""Orchestrator: open session → classify traffic → route to transport → download.

Pick order:
  1. If a site module (pyvid.sites.<host>) exists, let it choose / override.
  2. Otherwise pick the highest-scoring candidate from the sniffer:
     ump > dash = hls > progressive (with size tiebreaker).
"""
from __future__ import annotations

import asyncio
import importlib
import sys
from pathlib import Path
from urllib.parse import urlparse

from ..transports import progressive, hls, dash, vimeo, ump
from .session import open_session
from .sniffer import classify
from .types import Candidate, DownloadResult


async def download_video(
    url: str,
    output_dir: Path,
    *,
    headless: bool = False,
    idle_ms: int = 4000,
    max_ms: int = 120000,
    format_override: str | None = None,
) -> DownloadResult:
    site_mod = _load_site_module(url)

    # Full-override path: site modules may implement download_page() to take
    # complete control of session lifecycle (needed for sites like YouTube
    # where we must capture response bodies during playback).
    if site_mod is not None and hasattr(site_mod, "download_page"):
        print(f"[orch] site override: {site_mod.__name__}.download_page", file=sys.stderr)
        return await site_mod.download_page(
            url, output_dir,
            headless=headless, idle_ms=idle_ms, max_ms=max_ms,
            format_override=format_override,
        )

    async with open_session(
        url,
        headless=headless,
        idle_ms=idle_ms,
        max_ms=max_ms,
    ) as session:
        candidates = classify(session.captures)
        _print_candidates(candidates)

        chosen: Candidate | None
        if site_mod is not None and hasattr(site_mod, "choose_candidate"):
            result = site_mod.choose_candidate(candidates, session)
            chosen = await result if asyncio.iscoroutine(result) else result
        else:
            chosen = _default_pick(candidates, format_override)

        if chosen is None:
            raise RuntimeError(
                "no downloadable stream found. "
                "Either the site requires interaction, or the stream is DRM-protected, "
                "or this site needs a dedicated module in pyvid/sites/."
            )
        print(f"[orch] chosen: {chosen.kind} {chosen.url[:120]}", file=sys.stderr)

        transport_mod = {
            "progressive": progressive,
            "hls": hls,
            "dash": dash,
            "vimeo": vimeo,
            "ump": ump,
        }.get(chosen.kind)
        if transport_mod is None:
            raise RuntimeError(f"transport '{chosen.kind}' not registered")

        return await transport_mod.download(chosen, session.context, output_dir)


def _default_pick(candidates: list[Candidate], format_override: str | None) -> Candidate | None:
    if format_override:
        matches = [c for c in candidates if c.kind == format_override]
        return matches[0] if matches else None
    return candidates[0] if candidates else None


def _load_site_module(url: str):
    host = urlparse(url).hostname or ""
    parts = [p for p in host.split(".") if p and p not in ("www", "m")]
    # Try from most-specific to least-specific: e.g. ["vimeo","com"] then ["vimeo"].
    for depth in range(len(parts), 0, -1):
        name = ".".join(parts[:depth]).replace(".", "_")
        mod_name = f"pyvid.sites.{name}"
        try:
            return importlib.import_module(mod_name)
        except ModuleNotFoundError:
            continue
    return None


def _print_candidates(candidates: list[Candidate]) -> None:
    print(f"[orch] {len(candidates)} candidate(s):", file=sys.stderr)
    for i, c in enumerate(candidates[:10]):
        print(f"  {i:2d}. [{c.kind:11s}] score={c.score():<8d} {c.url[:120]}", file=sys.stderr)
    if len(candidates) > 10:
        print(f"  ... and {len(candidates) - 10} more", file=sys.stderr)
