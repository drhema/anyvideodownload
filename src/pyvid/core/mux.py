"""ffmpeg wrappers for the two cases we actually need.

- concat_files: join N segment files into one container without re-encoding.
- mux_tracks:   combine a separate video file + audio file into an MP4.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


class FFmpegMissing(RuntimeError):
    pass


def ensure_ffmpeg() -> str:
    ff = shutil.which("ffmpeg")
    if not ff:
        raise FFmpegMissing(
            "ffmpeg not found on PATH. Install with `brew install ffmpeg` on macOS."
        )
    return ff


def concat_files(parts: list[Path], output: Path, *, mode: str = "binary") -> Path:
    """Concatenate segments.

    mode='binary': append raw bytes (fine for MPEG-TS HLS segments that share a PID layout).
    mode='ffmpeg-concat': write a concat demuxer playlist and remux losslessly (for fMP4 .m4s).
    """
    output.parent.mkdir(parents=True, exist_ok=True)
    if not parts:
        raise ValueError("no parts to concat")

    if mode == "binary":
        with output.open("wb") as out:
            for p in parts:
                with p.open("rb") as f:
                    while True:
                        chunk = f.read(1 << 20)
                        if not chunk:
                            break
                        out.write(chunk)
        return output

    if mode == "ffmpeg-concat":
        ff = ensure_ffmpeg()
        listfile = output.with_suffix(".concat.txt")
        listfile.write_text(
            "\n".join(f"file '{p.resolve().as_posix()}'" for p in parts) + "\n"
        )
        cmd = [
            ff, "-hide_banner", "-loglevel", "error", "-y",
            "-f", "concat", "-safe", "0",
            "-i", str(listfile),
            "-c", "copy",
            str(output),
        ]
        subprocess.run(cmd, check=True)
        listfile.unlink(missing_ok=True)
        return output

    raise ValueError(f"unknown concat mode: {mode}")


def remux(input_file: Path, output: Path) -> Path:
    """Remux a single file into `output` (container change, no re-encode)."""
    ff = ensure_ffmpeg()
    output.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        ff, "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(input_file),
        "-c", "copy",
        "-bsf:a", "aac_adtstoasc",
        str(output),
    ]
    subprocess.run(cmd, check=True)
    return output


def mux_tracks(video: Path, audio: Path | None, output: Path) -> Path:
    """Combine a video file + optional audio file into `output` without re-encoding."""
    ff = ensure_ffmpeg()
    output.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        ff, "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(video),
    ]
    if audio is not None:
        cmd += ["-i", str(audio)]
    cmd += ["-c", "copy"]
    if audio is not None:
        cmd += ["-map", "0:v:0", "-map", "1:a:0"]
    cmd += [str(output)]
    subprocess.run(cmd, check=True)
    return output
