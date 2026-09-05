"""Microphone capture via ffmpeg.

Audio is captured in this process rather than in the task pane on purpose. Word task
panes run inside WebView2, where ``getUserMedia`` permission prompts are unreliable and
sometimes never appear at all — so the add-in would have no way to recover. ffmpeg
reading DirectShow sidesteps the whole problem and needs no Python audio bindings.

Output is always 16 kHz mono signed 16-bit little-endian, which is what both Silero VAD
and Whisper want.
"""
from __future__ import annotations

import asyncio
import logging
import re
import subprocess
from collections.abc import AsyncIterator

from service.config import AudioConfig

log = logging.getLogger(__name__)

_DEVICE_RE = re.compile(r'"([^"]+)"\s*\(audio\)')


def list_devices(ffmpeg: str = "ffmpeg") -> list[str]:
    """Enumerate DirectShow audio inputs.

    ffmpeg prints the list to stderr and then exits non-zero because ``dummy`` is not a
    real input; that is expected, so the return code is ignored.
    """
    try:
        proc = subprocess.run(
            [ffmpeg, "-hide_banner", "-list_devices", "true", "-f", "dshow", "-i", "dummy"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("could not enumerate audio devices: %s", exc)
        return []
    return _DEVICE_RE.findall(proc.stderr or "")


class MicStream:
    """Async iterator over fixed-size PCM frames from the microphone."""

    def __init__(self, cfg: AudioConfig) -> None:
        self.cfg = cfg
        self._proc: asyncio.subprocess.Process | None = None

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    def _args(self) -> list[str]:
        return [
            self.cfg.ffmpeg,
            "-hide_banner",
            "-loglevel", "error",
            "-nostdin",
            "-f", "dshow",
            # Small buffer: DirectShow defaults add hundreds of milliseconds of latency,
            # which lands directly on the user's perceived response time.
            "-audio_buffer_size", "50",
            "-i", f"audio={self.cfg.device}",
            "-ar", str(self.cfg.sample_rate),
            "-ac", "1",
            "-f", "s16le",
            "-",
        ]

    async def start(self) -> None:
        if self.running:
            return
        args = self._args()
        log.debug("starting capture: %s", " ".join(args))
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                stdin=asyncio.subprocess.DEVNULL,
            )
        except FileNotFoundError as exc:
            raise RuntimeError(
                f"ffmpeg not found at '{self.cfg.ffmpeg}'. Install it or set audio.ffmpeg in config.toml."
            ) from exc

        # Fail fast on a bad device name instead of hanging on an empty stdout.
        await asyncio.sleep(0.35)
        if self._proc.returncode is not None:
            stderr = b""
            if self._proc.stderr is not None:
                stderr = await self._proc.stderr.read()
            detail = stderr.decode("utf-8", "replace").strip().splitlines()
            hint = detail[-1] if detail else f"exit code {self._proc.returncode}"
            devices = ", ".join(list_devices(self.cfg.ffmpeg)) or "none found"
            self._proc = None
            raise RuntimeError(f"Microphone '{self.cfg.device}' failed: {hint}. Available: {devices}")

    async def stop(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None or proc.returncode is not None:
            return
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=3.0)
        except (asyncio.TimeoutError, ProcessLookupError):
            with_kill = getattr(proc, "kill", None)
            if with_kill:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass

    async def frames(self) -> AsyncIterator[bytes]:
        """Yield exactly ``frame_bytes`` per iteration until the stream ends."""
        await self.start()
        proc = self._proc
        assert proc is not None and proc.stdout is not None
        size = self.cfg.frame_bytes
        try:
            while True:
                chunk = await proc.stdout.readexactly(size)
                yield chunk
        except (asyncio.IncompleteReadError, asyncio.CancelledError):
            return
        finally:
            await self.stop()


def _main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Audio capture helper")
    parser.add_argument("--list", action="store_true", help="list DirectShow audio inputs")
    args = parser.parse_args()

    if args.list:
        devices = list_devices()
        if not devices:
            print("No DirectShow audio inputs found. Is ffmpeg on PATH?")
            return
        print("Audio inputs (copy one into config.toml as audio.device):")
        for name in devices:
            print(f"  {name}")


if __name__ == "__main__":
    _main()
