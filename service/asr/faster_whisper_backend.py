"""Whisper via CTranslate2 (faster-whisper), int8 on the CPU.

CTranslate2 has no ROCm backend, so this runs int8 on the CPU, which leaves the whole
GPU to the LLM. The two stages then overlap: the next utterance is transcribed while the
model is still writing the previous one.

The cost is almost entirely fixed rather than proportional to clip length, because
Whisper encodes a 30-second window however short the audio is. Measured here,
large-v3-turbo costs ~2.4 s per utterance whether the clip is 3 seconds or 16; beam size
and thread count move it by only a few percent, and faster-whisper's ``chunk_length``
does not help because CTranslate2 pads to 30 s regardless. Cutting this meaningfully
means a smaller model (see config.toml) or a GPU build of whisper.cpp.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import numpy as np

from service.asr import postfilter
from service.asr.base import Transcript
from service.config import AsrConfig

log = logging.getLogger(__name__)


class FasterWhisperTranscriber:
    name = "faster-whisper"

    def __init__(self, cfg: AsrConfig) -> None:
        self.cfg = cfg
        self._model: Any = None
        self._lock = asyncio.Lock()
        self._load_error = ""

    def state(self) -> dict[str, Any]:
        return {
            "asrBackend": self.name,
            "asrModel": self.cfg.model,
            "asrDevice": f"{self.cfg.device}/{self.cfg.compute_type}",
            "asrReady": self._model is not None,
            "asrError": self._load_error,
        }

    async def load(self) -> None:
        if self._model is not None:
            return
        async with self._lock:
            if self._model is not None:
                return
            try:
                self._model = await asyncio.to_thread(self._load_sync)
                self._load_error = ""
                log.info("ASR ready: %s (%s, %s)", self.cfg.model, self.cfg.device, self.cfg.compute_type)
            except Exception as exc:
                self._load_error = str(exc)
                log.error("ASR failed to load: %s", exc)
                raise

    def _load_sync(self) -> Any:
        from faster_whisper import WhisperModel

        log.info("loading Whisper '%s' (first run downloads the model)", self.cfg.model)
        return WhisperModel(
            self.cfg.model,
            device=self.cfg.device,
            compute_type=self.cfg.compute_type,
            cpu_threads=self.cfg.cpu_threads,
        )

    async def aclose(self) -> None:
        self._model = None

    async def transcribe(self, pcm: bytes, *, prompt: str = "") -> Transcript:
        await self.load()
        samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        duration_s = len(samples) / 16000.0
        started = time.perf_counter()
        # Serialised: CTranslate2 already uses every core, so concurrent calls would
        # only contend for them and make both slower.
        async with self._lock:
            text, info = await asyncio.to_thread(self._transcribe_sync, samples, prompt)
        latency_ms = (time.perf_counter() - started) * 1000

        transcript = Transcript(
            text=text.strip(),
            language=info.get("language", self.cfg.language),
            duration_s=duration_s,
            latency_ms=latency_ms,
            avg_logprob=info.get("avg_logprob", 0.0),
            no_speech_prob=info.get("no_speech_prob", 0.0),
        )
        transcript.dropped = postfilter.reason_to_drop(
            transcript.text,
            no_speech_prob=transcript.no_speech_prob,
            avg_logprob=transcript.avg_logprob,
            duration_s=duration_s,
        )
        if transcript.dropped:
            log.info("suppressed transcript (%s): %r", transcript.dropped, transcript.text[:80])
        return transcript

    def _transcribe_sync(self, samples: np.ndarray, prompt: str) -> tuple[str, dict[str, float | str]]:
        segments, info = self._model.transcribe(
            samples,
            language=self.cfg.language,
            beam_size=self.cfg.beam_size,
            # Our own Silero endpointer already cut this clip at a silence boundary.
            vad_filter=False,
            # Each utterance is independent: carrying the previous one's text forward is
            # the main cause of Whisper's runaway repetition loops.
            condition_on_previous_text=False,
            initial_prompt=prompt or None,
            without_timestamps=True,
        )

        parts: list[str] = []
        logprobs: list[float] = []
        no_speech: list[float] = []
        for segment in segments:            # generator: this is where inference happens
            parts.append(segment.text)
            logprobs.append(float(getattr(segment, "avg_logprob", 0.0) or 0.0))
            no_speech.append(float(getattr(segment, "no_speech_prob", 0.0) or 0.0))

        return " ".join(p.strip() for p in parts).strip(), {
            "language": getattr(info, "language", self.cfg.language),
            "avg_logprob": sum(logprobs) / len(logprobs) if logprobs else 0.0,
            "no_speech_prob": max(no_speech) if no_speech else 0.0,
        }
