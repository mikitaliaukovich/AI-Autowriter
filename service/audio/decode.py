"""Decoding audio files to the PCM the pipeline speaks.

Everything downstream expects 16 kHz mono signed 16-bit little-endian, the same format
the microphone capture produces, so a file can be pushed through the exact same code
path as live speech. ffmpeg does the conversion, which means any format it reads works
without adding a decoding dependency.
"""
from __future__ import annotations

import pathlib
import subprocess


def decode_to_pcm(path: str | pathlib.Path, ffmpeg: str = "ffmpeg", sample_rate: int = 16000) -> bytes:
    """Decode any audio file ffmpeg understands into raw mono PCM."""
    source = pathlib.Path(path)
    if not source.exists():
        raise FileNotFoundError(f"no such audio file: {source}")

    result = subprocess.run(
        [
            ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin",
            "-i", str(source),
            "-ar", str(sample_rate), "-ac", "1", "-f", "s16le", "-",
        ],
        capture_output=True,
        timeout=300,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", "replace").strip().splitlines()
        raise RuntimeError(f"ffmpeg could not decode {source.name}: {detail[-1] if detail else result.returncode}")
    return result.stdout
