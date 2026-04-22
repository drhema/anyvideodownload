"""YouTube UMP (User Media Protocol) transport — WORK IN PROGRESS.

This file is a scaffold, not a working transport yet. Documents the wire
format, provides a verified parser + stream demuxer, and raises
NotImplementedError for the remaining end-to-end steps.

UMP wire format (verified against real YouTube traffic, April 2026)
-------------------------------------------------------------------
A UMP response body is a sequence of *parts*:

    varint(part_type) varint(payload_len) payload_bytes

The varint is **NOT protobuf-style**. It uses UTF-8-like prefix encoding:

    0xxxxxxx                 → 1 byte,  value = byte
    10xxxxxx b               → 2 bytes, value = (b0 & 0x3f) | (b1 << 6)
    110xxxxx b b             → 3 bytes, (5 bits) | (16 bits little-endian)
    1110xxxx b b b           → 4 bytes, (4 bits) | (24 bits little-endian)
    11110xxx b b b b         → 5 bytes, (3 bits) | (32 bits little-endian)

(Same encoding as `googlevideo.js`. The leading-1s count determines byte length;
subsequent bytes are little-endian.)

Part types observed in the wild
-------------------------------
  20   ONESIE_HEADER            — metadata header for the ONESIE delivery session
  21   ONESIE_DATA              — media chunk; see "Onesie framing" below
  22   (seen, tag22)            — short 1-byte marker; role unclear
  35   MEDIA_HEADER             — protobuf: stream_id, itag, content_length, etc.
  36   MEDIA                    — raw fMP4/WebM segment (for non-onesie clients)
  37   MEDIA_END                — end-of-stream marker
  42   STREAM_PROTECTION_STATUS — PoToken status
  47, 52, 53, 58               — short framing/metadata, not yet identified
  43   SABR_ERROR
  44   SABR_SEEK
  45   LIVE_METADATA
  46   HOSTNAME_CHANGE

Onesie framing — how media actually comes out
---------------------------------------------
The WEB client uses ONESIE_DATA, NOT MEDIA, to carry media. Each ONESIE_DATA
payload is:

    stream_id_byte (1 byte)   payload (N-1 bytes)

Different stream_ids are multiplexed in a single HTTP response. The server
delivers several renditions at once (client chooses which to keep). Grouping
ONESIE_DATA payloads by their leading stream_id byte and concatenating yields
independent WebM (or fMP4) streams per rendition.

In ump_000.bin (418 KB) we see 7 distinct stream_ids, two of which begin with
EBML magic (WebM init segments) — confirming this is VP9/Opus WebM.

What's still needed for end-to-end download
-------------------------------------------
[ ] Decode MEDIA_HEADER protobuf — fields needed: stream_id → itag/format_id
    mapping, content_length, sequence number, is_init_segment flag.
[ ] Call /youtubei/v1/player on the target video to enumerate available
    itags (itag table: 243=240p vp9, 244=480p vp9, 299=1080p30 avc1, etc.).
[ ] Construct SABR POST request that requests a specific format.
[ ] Handle PoToken. Options: (a) extract from the live browser page state,
    (b) use TVHTML5_SIMPLY_EMBEDDED_PLAYER client which skips PoToken on
    many videos, (c) wire an external PoToken generator.
[ ] Handle `n` param deobfuscation — eval the scrambling function from
    the page's base.js in a JS sandbox (can use Playwright's page.evaluate
    since we already have a browser session).
[ ] Reassemble segments per stream_id → remux to MP4 (audio + video).

For working YouTube downloads today, use yt-dlp.
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterator

from playwright.async_api import BrowserContext

from ..core.types import Candidate, DownloadResult


def parse_varint(buf: bytes, offset: int) -> tuple[int, int]:
    """Decode a YouTube UMP-style varint starting at `offset`.

    NOT protobuf: UMP uses UTF-8-like prefix encoding. The leading 1-bits
    of the first byte (followed by a zero) tell you the total byte count:

        0xxxxxxx                 → 1 byte, value = byte
        10xxxxxx yyyyyyyy        → 2 bytes, value = (byte & 0x3f) | (byte2 << 6)
        110xxxxx y y             → 3 bytes, value = (byte & 0x1f) | (next bytes LE << 5)
        1110xxxx y y y           → 4 bytes  (4 bits from first + 24 from rest)
        11110xxx y y y y         → 5 bytes  (3 bits from first + 32 from rest)

    Subsequent bytes are little-endian (LSB first). Returns (value, bytes_consumed).
    """
    if offset >= len(buf):
        raise ValueError(f"varint truncated at offset {offset}")
    first = buf[offset]
    if (first & 0x80) == 0:
        return first, 1
    if (first & 0x40) == 0:
        size, value_bits_in_first = 2, 6
    elif (first & 0x20) == 0:
        size, value_bits_in_first = 3, 5
    elif (first & 0x10) == 0:
        size, value_bits_in_first = 4, 4
    else:
        size, value_bits_in_first = 5, 3
    if offset + size > len(buf):
        raise ValueError(f"varint truncated at offset {offset}")
    mask = (1 << value_bits_in_first) - 1
    value = first & mask
    for i in range(1, size):
        value |= buf[offset + i] << (value_bits_in_first + (i - 1) * 8)
    return value, size


def iter_ump_parts(body: bytes) -> Iterator[tuple[int, bytes]]:
    """Yield (part_type, payload) for each UMP frame in `body`.

    Tolerates trailing garbage; stops cleanly on truncation.
    """
    pos = 0
    while pos < len(body):
        try:
            part_type, n1 = parse_varint(body, pos)
            payload_len, n2 = parse_varint(body, pos + n1)
        except ValueError:
            return
        header = n1 + n2
        start = pos + header
        end = start + payload_len
        if end > len(body):
            return
        yield part_type, body[start:end]
        pos = end


# Human-readable tag names for debugging dumps.
UMP_TAGS: dict[int, str] = {
    20: "ONESIE_HEADER",
    21: "ONESIE_DATA",
    22: "ONESIE_MARKER?",
    35: "MEDIA_HEADER",
    36: "MEDIA",
    37: "MEDIA_END",
    42: "STREAM_PROTECTION_STATUS",
    43: "SABR_ERROR",
    44: "SABR_SEEK",
    45: "LIVE_METADATA",
    46: "HOSTNAME_CHANGE",
}


def demux_onesie(bodies: list[bytes]) -> dict[int, bytes]:
    """Group ONESIE_DATA payloads by stream_id (first byte) and concatenate.

    Pass all captured UMP response bodies for a session; returns a dict
    mapping stream_id → concatenated bytes (a WebM/fMP4 per stream).
    """
    from collections import defaultdict
    streams: dict[int, bytearray] = defaultdict(bytearray)
    for body in bodies:
        for part_type, payload in iter_ump_parts(body):
            if part_type == 21 and len(payload) > 0:
                stream_id = payload[0]
                streams[stream_id].extend(payload[1:])
    return {sid: bytes(data) for sid, data in streams.items()}


def demux_media(bodies: list[bytes]) -> list[bytes]:
    """Collect all tag-36 (MEDIA) payloads — the non-onesie delivery mode."""
    out: list[bytes] = []
    for body in bodies:
        for part_type, payload in iter_ump_parts(body):
            if part_type == 36:
                out.append(payload)
    return out


def describe_body(body: bytes) -> str:
    """Human-readable summary of a UMP body. Useful for fixture analysis."""
    lines = [f"total bytes: {len(body)}"]
    counts: dict[int, int] = {}
    total_payload: dict[int, int] = {}
    for part_type, payload in iter_ump_parts(body):
        counts[part_type] = counts.get(part_type, 0) + 1
        total_payload[part_type] = total_payload.get(part_type, 0) + len(payload)
    for pt in sorted(counts):
        name = UMP_TAGS.get(pt, f"tag{pt}")
        lines.append(f"  {pt:3d} {name:<28s} count={counts[pt]:<4d} payload_bytes={total_payload[pt]}")
    return "\n".join(lines)


async def download(
    candidate: Candidate,
    ctx: BrowserContext,
    output_dir: Path,
) -> DownloadResult:
    raise NotImplementedError(
        "YouTube UMP download is not yet implemented. "
        "The wire parser (iter_ump_parts) works; what's still missing: "
        "(1) /youtubei/v1/player client, (2) SABR request construction, "
        "(3) segment reassembly, (4) PoToken plumbing, (5) n-param transform, "
        "(6) video+audio mux. See module docstring for the full plan. "
        "For working YouTube downloads today, use yt-dlp."
    )
