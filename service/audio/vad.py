"""Voice activity detection and utterance endpointing.

Endpointing is what turns a continuous microphone stream into discrete utterances, and
its parameters are felt directly by the user: too short a silence window chops sentences
mid-thought, too long and every phrase feels laggy.

Silero VAD (ONNX, ~2 MB, CPU) is the default because it distinguishes speech from
keyboard clatter and room noise far better than an energy threshold. The energy
detector remains as a fallback so the system still works if onnxruntime is unavailable.
"""
from __future__ import annotations

import logging
import pathlib
import urllib.error
import urllib.request

import numpy as np

from service.config import AudioConfig, VadConfig

log = logging.getLogger(__name__)

SILERO_URL = "https://raw.githubusercontent.com/snakers4/silero-vad/master/src/silero_vad/data/silero_vad.onnx"
SILERO_FILENAME = "silero_vad.onnx"

# Silero v5 is trained on exactly this window at 16 kHz and silently misbehaves on others.
SILERO_WINDOW = 512


def ensure_model(models_dir: pathlib.Path) -> pathlib.Path:
    """Download the Silero VAD model once. Everything else runs offline."""
    models_dir.mkdir(parents=True, exist_ok=True)
    path = models_dir / SILERO_FILENAME
    if path.exists() and path.stat().st_size > 1000:
        return path
    log.info("downloading Silero VAD model (one time, ~2 MB)")
    try:
        with urllib.request.urlopen(SILERO_URL, timeout=60) as response:
            data = response.read()
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError(f"could not download Silero VAD: {exc}") from exc
    path.write_bytes(data)
    return path


class SileroVad:
    """Frame-level speech probability."""

    def __init__(self, model_path: pathlib.Path, sample_rate: int = 16000) -> None:
        import onnxruntime  # imported lazily so the fallback path needs no dependency

        options = onnxruntime.SessionOptions()
        options.inter_op_num_threads = 1
        options.intra_op_num_threads = 1
        options.log_severity_level = 3
        self._session = onnxruntime.InferenceSession(
            str(model_path), sess_options=options, providers=["CPUExecutionProvider"]
        )
        self._input_names = {i.name for i in self._session.get_inputs()}
        self.sample_rate = sample_rate
        self._state = np.zeros((2, 1, 128), dtype=np.float32)

    def reset(self) -> None:
        self._state = np.zeros((2, 1, 128), dtype=np.float32)

    def probability(self, samples: np.ndarray) -> float:
        """Speech probability for one 512-sample float32 window in [-1, 1]."""
        if samples.shape[0] != SILERO_WINDOW:
            if samples.shape[0] > SILERO_WINDOW:
                samples = samples[:SILERO_WINDOW]
            else:
                samples = np.pad(samples, (0, SILERO_WINDOW - samples.shape[0]))

        feeds = {
            "input": samples.reshape(1, -1).astype(np.float32),
            "sr": np.array(self.sample_rate, dtype=np.int64),
        }
        if "state" in self._input_names:
            feeds["state"] = self._state
        outputs = self._session.run(None, feeds)
        if len(outputs) > 1:
            self._state = outputs[1]
        return float(np.asarray(outputs[0]).ravel()[0])


class EnergyVad:
    """Fallback: adaptive RMS threshold with a slowly-tracked noise floor."""

    def __init__(self, sample_rate: int = 16000) -> None:
        self.sample_rate = sample_rate
        self._floor = 0.005

    def reset(self) -> None:
        self._floor = 0.005

    def probability(self, samples: np.ndarray) -> float:
        rms = float(np.sqrt(np.mean(np.square(samples))) + 1e-9)
        if rms < self._floor * 2:
            self._floor = 0.98 * self._floor + 0.02 * rms
        ratio = rms / max(self._floor, 1e-5)
        # Map a 3x-over-noise-floor ratio onto roughly 0.5 probability.
        return float(min(1.0, max(0.0, (ratio - 1.5) / 3.0)))


def make_vad(cfg: VadConfig, audio: AudioConfig, models_dir: pathlib.Path) -> SileroVad | EnergyVad:
    if cfg.backend == "silero":
        try:
            return SileroVad(ensure_model(models_dir), audio.sample_rate)
        except Exception as exc:
            log.warning("Silero VAD unavailable (%s); falling back to energy detection", exc)
    return EnergyVad(audio.sample_rate)


class Endpointer:
    """Accumulates frames and emits complete utterances.

    Keeps a pre-roll ring buffer so the beginning of the first word is not clipped —
    VAD always reacts a frame or two late, and losing the initial consonant of a Russian
    word is enough to change what Whisper hears.
    """

    def __init__(self, vad: SileroVad | EnergyVad, cfg: VadConfig, audio: AudioConfig) -> None:
        self.vad = vad
        self.cfg = cfg
        self.audio = audio
        self._frame_ms = audio.frame_ms
        self._preroll_frames = max(1, cfg.preroll_ms // audio.frame_ms)
        self._preroll: list[bytes] = []
        self._buffer: list[bytes] = []
        self._speech_ms = 0
        self._silence_ms = 0
        self._in_speech = False

    def reset(self) -> None:
        self.vad.reset()
        self._preroll.clear()
        self._buffer.clear()
        self._speech_ms = 0
        self._silence_ms = 0
        self._in_speech = False

    @property
    def in_speech(self) -> bool:
        return self._in_speech

    def push(self, frame: bytes) -> bytes | None:
        """Feed one frame. Returns a complete utterance's PCM when one ends."""
        samples = np.frombuffer(frame, dtype=np.int16).astype(np.float32) / 32768.0
        speech = self.vad.probability(samples) >= self.cfg.threshold

        if not self._in_speech:
            self._preroll.append(frame)
            if len(self._preroll) > self._preroll_frames:
                self._preroll.pop(0)
            if speech:
                self._in_speech = True
                self._buffer = [*self._preroll, ]
                self._preroll = []
                self._speech_ms = self._frame_ms
                self._silence_ms = 0
            return None

        self._buffer.append(frame)
        if speech:
            self._speech_ms += self._frame_ms
            self._silence_ms = 0
        else:
            self._silence_ms += self._frame_ms

        duration_ms = len(self._buffer) * self._frame_ms
        ended = self._silence_ms >= self.cfg.silence_ms
        overlong = duration_ms >= self.cfg.max_utterance_ms
        if not (ended or overlong):
            return None

        audio = b"".join(self._buffer)
        speech_ms = self._speech_ms
        self.reset()
        if overlong:
            # Keep listening: the speaker has not stopped, they have just gone on a while.
            self._in_speech = True
            self._buffer = []
        if speech_ms < self.cfg.min_speech_ms:
            return None
        return audio

    def flush(self) -> bytes | None:
        """Emit whatever is buffered, e.g. when the user toggles the mic off mid-sentence."""
        if not self._in_speech or self._speech_ms < self.cfg.min_speech_ms:
            self.reset()
            return None
        audio = b"".join(self._buffer)
        self.reset()
        return audio
