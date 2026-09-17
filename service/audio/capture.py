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
import contextlib
import logging
import re
import subprocess
from collections.abc import AsyncIterator

from service.config import AudioConfig

log = logging.getLogger(__name__)

_DEVICE_RE = re.compile(r'"([^"]+)"\s*\(audio\)')

_CHANNEL_NAMES = {"left": 0, "l": 0, "right": 1, "r": 1}


def channel_index(channel: str) -> int | None:
    """Resolve a channel setting to an index, or None for the default downmix."""
    key = (channel or "mix").strip().lower()
    if key in ("", "mix", "both", "auto"):
        return None
    if key in _CHANNEL_NAMES:
        return _CHANNEL_NAMES[key]
    if key.isdigit():
        return int(key)
    log.warning("unknown audio.channel %r; falling back to a downmix", channel)
    return None


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
        self._drain: asyncio.Task[None] | None = None

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    def filter_chain(self) -> str:
        """The -af value implementing the configured channel choice and gain."""
        stages: list[str] = []
        index = channel_index(self.cfg.channel)
        if index is not None:
            # Take one channel outright rather than downmixing: averaging a live channel
            # with a silent one halves the amplitude.
            stages.append(f"pan=mono|c0=c{index}")
        if self.cfg.gain_db:
            stages.append(f"volume={self.cfg.gain_db}dB")
        return ",".join(stages)

    def _args(self) -> list[str]:
        args = [
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
        ]
        chain = self.filter_chain()
        if chain:
            args += ["-af", chain]
        args += ["-ac", "1", "-f", "s16le", "-"]
        return args

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

        self._drain = asyncio.create_task(self._drain_stderr(self._proc), name="ffmpeg-stderr")

    async def stop(self) -> None:
        proc, self._proc = self._proc, None
        drain, self._drain = self._drain, None
        if drain is not None:
            drain.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await drain

        if proc is None or proc.returncode is not None:
            return

        with contextlib.suppress(ProcessLookupError, OSError):
            proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=3.0)
            return
        except (asyncio.TimeoutError, TimeoutError):
            log.warning("ffmpeg ignored terminate; killing it")
        except ProcessLookupError:
            return

        with contextlib.suppress(ProcessLookupError, OSError):
            proc.kill()
        # Reap it, otherwise the child lingers holding the microphone open.
        with contextlib.suppress(asyncio.TimeoutError, TimeoutError, ProcessLookupError):
            await asyncio.wait_for(proc.wait(), timeout=3.0)

    async def _drain_stderr(self, proc: asyncio.subprocess.Process) -> None:
        """Keep ffmpeg's stderr pipe empty.

        Nothing reads it during normal operation, and a pipe nobody drains eventually
        fills — at which point ffmpeg blocks on write and stops producing audio, with no
        error anywhere to explain it.
        """
        if proc.stderr is None:
            return
        try:
            while True:
                line = await proc.stderr.readline()
                if not line:
                    return
                text = line.decode("utf-8", "replace").strip()
                if text:
                    log.warning("ffmpeg: %s", text)
        except (asyncio.CancelledError, asyncio.IncompleteReadError):
            raise
        except Exception:
            return

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
