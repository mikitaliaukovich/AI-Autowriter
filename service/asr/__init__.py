"""Speech recognition backends, selected by ``asr.backend`` in config.toml."""
from __future__ import annotations

from service.asr.base import Transcriber, Transcript
from service.config import AsrConfig

__all__ = ["Transcriber", "Transcript", "make_transcriber"]


def make_transcriber(cfg: AsrConfig) -> Transcriber:
    if cfg.backend == "whisper.cpp":
        from service.asr.whispercpp_backend import WhisperCppTranscriber

        return WhisperCppTranscriber(cfg)

    from service.asr.faster_whisper_backend import FasterWhisperTranscriber

    return FasterWhisperTranscriber(cfg)
