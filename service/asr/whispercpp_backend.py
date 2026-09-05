"""Whisper via an external whisper.cpp HTTP server.

Optional alternative to the CPU backend, for running ASR on the GPU through
whisper.cpp's Vulkan build. Start the server separately, for example::

    whisper-server.exe -m ggml-large-v3-turbo.bin -l ru --host 127.0.0.1 --port 8080

then set ``backend = "whisper.cpp"`` in ``config.toml``. Same interface as the CPU
backend, so nothing else in the pipeline changes.
"""
from __future__ import annotations

import io
import logging
import time
import wave
from typing import Any

import httpx

from service.asr import postfilter
from service.asr.base import Transcript
from service.config import AsrConfig

log = logging.getLogger(__name__)


def pcm_to_wav(pcm: bytes, sample_rate: int = 16000) -> bytes:
    """Wrap raw mono 16-bit PCM in a WAV container."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(pcm)
    return buffer.getvalue()


class WhisperCppTranscriber:
    name = "whisper.cpp"

    def __init__(self, cfg: AsrConfig) -> None:
        self.cfg = cfg
        self._client = httpx.AsyncClient(base_url=cfg.server_url.rstrip("/"), timeout=120.0)
        self._ready = False
        self._error = ""

    def state(self) -> dict[str, Any]:
        return {
            "asrBackend": self.name,
            "asrModel": self.cfg.model,
            "asrDevice": self.cfg.server_url,
            "asrReady": self._ready,
            "asrError": self._error,
        }

    async def load(self) -> None:
        try:
            response = await self._client.get("/", timeout=5.0)
            self._ready = response.status_code < 500
            self._error = "" if self._ready else f"server returned {response.status_code}"
        except Exception as exc:
            self._ready = False
            self._error = f"whisper.cpp server unreachable at {self.cfg.server_url}: {exc}"
            log.error("%s", self._error)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def transcribe(self, pcm: bytes, *, prompt: str = "") -> Transcript:
        duration_s = len(pcm) / 2 / 16000.0
        started = time.perf_counter()
        files = {"file": ("audio.wav", pcm_to_wav(pcm), "audio/wav")}
        data = {
            "temperature": "0.0",
            "response_format": "json",
            "language": self.cfg.language,
        }
        if prompt:
            data["prompt"] = prompt

        try:
            response = await self._client.post("/inference", files=files, data=data)
            response.raise_for_status()
            text = str(response.json().get("text") or "").strip()
            self._ready, self._error = True, ""
        except Exception as exc:
            self._ready = False
            self._error = str(exc)
            log.error("whisper.cpp request failed: %s", exc)
            return Transcript(duration_s=duration_s, dropped=f"asr error: {exc}")

        transcript = Transcript(
            text=text,
            language=self.cfg.language,
            duration_s=duration_s,
            latency_ms=(time.perf_counter() - started) * 1000,
        )
        transcript.dropped = postfilter.reason_to_drop(text, duration_s=duration_s)
        return transcript
