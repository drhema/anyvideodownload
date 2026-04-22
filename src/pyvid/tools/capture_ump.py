"""Capture raw YouTube UMP response bodies to disk for offline analysis.

Usage:
    python -m pyvid.tools.capture_ump "https://www.youtube.com/watch?v=..." [outdir]

Writes every videoplayback response body to `outdir/ump_<i>.bin` plus a
`summary.txt` with tag histograms produced by ump.describe_body().
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from ..core.session import open_session
from ..transports.ump import describe_body


async def _run(url: str, outdir: Path) -> None:
    outdir.mkdir(parents=True, exist_ok=True)

    captured: list[tuple[str, bytes]] = []

    async def on_response(res, _cap) -> None:
        if "googlevideo.com/videoplayback" not in res.url:
            return
        try:
            body = await res.body()
        except Exception:
            return
        if body:
            captured.append((res.url, body))

    async with open_session(
        url, headless=False, idle_ms=6000, max_ms=90000,
        on_response=on_response,
    ):
        pass

    print(f"[capture-ump] got {len(captured)} videoplayback bodies", file=sys.stderr)
    summary_lines: list[str] = []
    for i, (u, body) in enumerate(captured):
        path = outdir / f"ump_{i:03d}.bin"
        path.write_bytes(body)
        summary_lines.append(f"\n--- ump_{i:03d}.bin ({len(body)} bytes) ---")
        summary_lines.append(f"url: {u[:200]}")
        summary_lines.append(describe_body(body))

    (outdir / "summary.txt").write_text("\n".join(summary_lines))
    print(f"[capture-ump] wrote {len(captured)} bodies + summary.txt to {outdir}", file=sys.stderr)


def main() -> None:
    if len(sys.argv) < 2:
        print("usage: python -m pyvid.tools.capture_ump <youtube-url> [outdir]", file=sys.stderr)
        sys.exit(1)
    url = sys.argv[1]
    outdir = Path(sys.argv[2] if len(sys.argv) > 2 else "./ump_captures")
    asyncio.run(_run(url, outdir))


if __name__ == "__main__":
    main()
