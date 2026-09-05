"""Common types for speech recognition backends."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass
class Transcript:
    text: str = ""
    language: str = ""
    duration_s: float = 0.0
    latency_ms: float = 0.0
    avg_logprob: float = 0.0
    no_speech_prob: float = 0.0
    dropped: str = ""          # why the text was suppressed, if it was
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return bool(self.text) and not self.dropped

    @property
    def realtime_factor(self) -> float:
        """Audio seconds processed per wall-clock second. Above 1 is faster than real time."""
        if self.latency_ms <= 0:
            return 0.0
        return self.duration_s / (self.latency_ms / 1000)


@runtime_checkable
class Transcriber(Protocol):
    name: str

    async def load(self) -> None:
        """Load the model. Safe to call more than once."""

    async def transcribe(self, pcm: bytes, *, prompt: str = "") -> Transcript:
        """Transcribe 16 kHz mono signed 16-bit little-endian PCM."""

    async def aclose(self) -> None:
        ...

    def state(self) -> dict[str, Any]:
        """Readiness information for the task pane's status strip."""
