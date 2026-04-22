"""Playwright session wrapper.

Opens a real Chromium, navigates to the target URL, records every network
response via a callback. Exposes the browser context's cookies and headers so
that segment fetches later can reuse the authenticated session.
"""
from __future__ import annotations

import asyncio
import sys
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from playwright.async_api import (
    BrowserContext,
    Page,
    Request,
    Response,
    async_playwright,
)

from .types import CapturedRequest


@dataclass
class SessionState:
    context: BrowserContext
    page: Page
    captures: list[CapturedRequest] = field(default_factory=list)
    bodies: dict[str, bytes] = field(default_factory=dict)   # url -> body (small text responses only)


@asynccontextmanager
async def open_session(
    url: str,
    *,
    headless: bool = False,
    idle_ms: int = 4000,
    max_ms: int = 120000,
    capture_bodies_re: str | None = None,
    on_response: Callable[[Response, CapturedRequest], Any] | None = None,
):
    """Async context manager that yields a populated SessionState.

    The session navigates to `url`, tries to autoplay any <video>, and waits
    until network has been quiet for `idle_ms` (or `max_ms` hard cap), then
    hands control back. The browser remains open while the context is live —
    transports can use `state.context` / `state.page` to make additional
    authenticated requests.
    """
    import os
    import re
    import shlex
    body_re = re.compile(capture_bodies_re) if capture_bodies_re else None

    # Extra Chromium args from env — needed in Docker (--no-sandbox,
    # --disable-dev-shm-usage), proxy configs, etc.
    extra_args = shlex.split(os.environ.get("PYVID_CHROMIUM_ARGS", ""))

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=headless,
            args=["--autoplay-policy=no-user-gesture-required", *extra_args],
        )
        ctx = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 800},
            ignore_https_errors=True,
        )
        page = await ctx.new_page()
        state = SessionState(context=ctx, page=page)

        last_activity = {"t": asyncio.get_event_loop().time()}

        def touch():
            last_activity["t"] = asyncio.get_event_loop().time()

        async def handle_response(res: Response) -> None:
            try:
                req = res.request
                headers = await res.all_headers()
                ct = (headers.get("content-type") or "").split(";")[0].strip()
                length_hdr = headers.get("content-length")
                content_range = headers.get("content-range", "")
                size = 0
                if length_hdr and length_hdr.isdigit():
                    size = int(length_hdr)
                else:
                    import re as _re
                    m = _re.search(r"bytes\s+\d+-\d+/(\d+)", content_range)
                    if m:
                        size = int(m.group(1))
                cap = CapturedRequest(
                    url=res.url,
                    method=req.method,
                    status=res.status,
                    content_type=ct,
                    size=size,
                    resource_type=req.resource_type,
                    headers=dict(headers),
                    request_headers=dict(await req.all_headers()),
                )
                state.captures.append(cap)
                touch()

                if body_re and body_re.search(res.url):
                    try:
                        state.bodies[res.url] = await res.body()
                    except Exception:
                        pass

                if on_response is not None:
                    result = on_response(res, cap)
                    if asyncio.iscoroutine(result):
                        await result
            except Exception as e:
                print(f"[session] response handler error: {e}", file=sys.stderr)

        def handle_request(_req: Request) -> None:
            touch()

        page.on("request", handle_request)
        page.on("response", lambda r: asyncio.create_task(handle_response(r)))

        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            print(f"[session] goto warning (continuing): {e}", file=sys.stderr)

        # Nudge playback so segment requests actually fire. Three strategies:
        #   1. Call .play() on every <video> element (works for native / <video src> sites).
        #   2. Click any visible element that looks like a play button (custom players).
        #   3. Repeat after 2s, for players that mount lazily after DCL.
        nudge_script = r"""
            (() => {
              const playVideos = () => document.querySelectorAll('video').forEach(v => {
                try { v.muted = true; v.play().catch(()=>{}); } catch(e){}
              });
              const clickPlay = () => {
                const sels = [
                  'button[aria-label*="play" i]',
                  'button[title*="play" i]',
                  'button[data-testid*="play" i]',
                  '[class*="play-button" i]',
                  '[class*="PlayButton" i]',
                  '[class*="vjs-big-play-button"]',
                  '[class*="plyr__control--overlaid"]',
                ];
                for (const s of sels) {
                  document.querySelectorAll(s).forEach(el => {
                    try {
                      const r = el.getBoundingClientRect();
                      if (r.width > 0 && r.height > 0) el.click();
                    } catch(e){}
                  });
                }
              };
              playVideos();
              clickPlay();
              setTimeout(() => { playVideos(); clickPlay(); }, 2000);
            })();
        """
        try:
            await page.evaluate(nudge_script)
        except Exception:
            pass

        loop = asyncio.get_event_loop()
        started = loop.time()
        while loop.time() - started < max_ms / 1000:
            if loop.time() - last_activity["t"] > idle_ms / 1000:
                break
            await asyncio.sleep(0.25)

        try:
            yield state
        finally:
            await ctx.close()
            await browser.close()


async def cookies_as_header(ctx: BrowserContext, url: str) -> str:
    """Return a Cookie header string suitable for an httpx request to `url`."""
    cookies = await ctx.cookies(url)
    return "; ".join(f"{c['name']}={c['value']}" for c in cookies)
