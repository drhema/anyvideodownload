from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from .core.orchestrator import download_video


def main() -> None:
    p = argparse.ArgumentParser(
        prog="pyvid",
        description="Browser-intercept video downloader (progressive / HLS / DASH; YouTube UMP WIP).",
    )
    p.add_argument("url", help="page URL that hosts the video")
    p.add_argument("-o", "--out", default="./downloads", help="output directory (default ./downloads)")
    p.add_argument("--headless", action="store_true", help="run headless (many sites block this)")
    p.add_argument("--idle-ms", type=int, default=4000,
                   help="wait this long without network activity before capturing ends (default 4000)")
    p.add_argument("--max-ms", type=int, default=120000,
                   help="hard cap on capture duration in ms (default 120000)")
    p.add_argument("--format", choices=["progressive", "hls", "dash", "vimeo"],
                   help="force a specific transport kind")
    p.add_argument("--dry-run", action="store_true",
                   help="open the page, classify traffic, print candidates, then exit without downloading")
    p.add_argument("--dump", metavar="PATH",
                   help="with --dry-run, also dump every captured request to this file (text)")
    args = p.parse_args()

    out = Path(args.out).expanduser().resolve()

    if args.dry_run:
        asyncio.run(_dry_run(args.url, headless=args.headless, idle_ms=args.idle_ms,
                             max_ms=args.max_ms, dump_path=args.dump))
        return

    try:
        result = asyncio.run(
            download_video(
                args.url,
                out,
                headless=args.headless,
                idle_ms=args.idle_ms,
                max_ms=args.max_ms,
                format_override=args.format,
            )
        )
    except Exception as e:
        print(f"[pyvid] error: {e}", file=sys.stderr)
        sys.exit(1)

    print(result.output_path)


async def _dry_run(url: str, *, headless: bool, idle_ms: int, max_ms: int,
                   dump_path: str | None = None) -> None:
    from .core.session import open_session
    from .core.sniffer import classify

    async with open_session(url, headless=headless, idle_ms=idle_ms, max_ms=max_ms) as session:
        candidates = classify(session.captures)
        print(f"[dry-run] {len(candidates)} candidate(s):")
        for i, c in enumerate(candidates):
            print(f"  {i:2d}. [{c.kind:11s}] score={c.score():<8d} {c.url}")

        if dump_path:
            with open(dump_path, "w") as f:
                f.write(f"# full capture — {len(session.captures)} requests\n\n")
                for cap in session.captures:
                    f.write(
                        f"[{cap.status or '?':>3}] {cap.method:<6} "
                        f"ct={cap.content_type or '-':<30} size={cap.size:<10} "
                        f"{cap.url}\n"
                    )
            print(f"[dry-run] full capture dumped to {dump_path}")


if __name__ == "__main__":
    main()
