"""Voice activity detection and endpointing, against a real speech recording.

These tests exist because of a bug that was invisible from the outside: Silero v5 wants
the previous chunk's last 64 samples prepended to each window, and omitting that does
not raise — the model simply returns near-zero for everything. The microphone worked,
the level meter worked, and dictation silently did nothing, because no utterance was
ever closed.

So the assertions here are behavioural: real speech must be *detected*, not merely
processed without error.
"""
from __future__ import annotations

import pathlib
import wave

import numpy as np
import pytest

from service.config import AudioConfig, VadConfig
from service.audio.vad import (
    SILERO_CONTEXT,
    SILERO_WINDOW,
    Endpointer,
    EnergyVad,
    SileroVad,
    ensure_model,
)

FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "speech_16k_mono.wav"
MODELS = pathlib.Path(__file__).parent.parent / "models"

AUDIO = AudioConfig(sample_rate=16000, frame_ms=32)
VAD = VadConfig()


def read_fixture() -> np.ndarray:
    with wave.open(str(FIXTURE), "rb") as handle:
        assert handle.getnchannels() == 1
        assert handle.getframerate() == 16000
        raw = handle.readframes(handle.getnframes())
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


def silence(seconds: float) -> np.ndarray:
    return np.zeros(int(16000 * seconds), dtype=np.float32)


def to_pcm(samples: np.ndarray) -> bytes:
    return (np.clip(samples, -1, 1) * 32767).astype(np.int16).tobytes()


def probabilities(vad, samples: np.ndarray) -> np.ndarray:
    count = len(samples) // SILERO_WINDOW
    return np.array([
        vad.probability(samples[i * SILERO_WINDOW:(i + 1) * SILERO_WINDOW]) for i in range(count)
    ])


@pytest.fixture(scope="module")
def silero() -> SileroVad:
    try:
        return SileroVad(ensure_model(MODELS))
    except Exception as exc:                                  # pragma: no cover
        pytest.skip(f"Silero VAD unavailable: {exc}")


class TestSileroDetectsSpeech:
    """The regression guard. Without the 64-sample context these all collapse to ~0."""

    def test_speech_is_detected(self, silero: SileroVad) -> None:
        silero.reset()
        probs = probabilities(silero, read_fixture())
        assert probs.max() > 0.9, f"peak probability was only {probs.max():.3f}"
        assert (probs >= VAD.threshold).mean() > 0.3, "speech should dominate the clip"

    def test_silence_is_not_detected(self, silero: SileroVad) -> None:
        silero.reset()
        probs = probabilities(silero, silence(2.0))
        assert probs.max() < 0.2, f"silence scored {probs.max():.3f}"

    def test_speech_scores_far_above_silence(self, silero: SileroVad) -> None:
        silero.reset()
        speech = probabilities(silero, read_fixture()).mean()
        silero.reset()
        quiet = probabilities(silero, silence(2.0)).mean()
        assert speech > quiet * 10

    def test_context_window_is_carried_between_calls(self, silero: SileroVad) -> None:
        silero.reset()
        assert silero._context.shape == (1, SILERO_CONTEXT)
        assert not silero._context.any(), "context starts empty"

        # Take a window from the middle of the clip: the recording opens with digital
        # silence, whose tail is indistinguishable from an uncarried context.
        window = read_fixture()[SILERO_WINDOW * 20:SILERO_WINDOW * 21]
        silero.probability(window)
        assert np.allclose(silero._context[0], window[-SILERO_CONTEXT:]), (
            "context must hold the tail of the window just processed"
        )

    def test_reset_clears_the_carried_state(self, silero: SileroVad) -> None:
        silero.probability(read_fixture()[:SILERO_WINDOW])
        silero.reset()
        assert not silero._context.any()
        assert not silero._state.any()


class TestEnergyFallback:
    def test_detects_speech(self) -> None:
        probs = probabilities(EnergyVad(), read_fixture())
        assert (probs >= VAD.threshold).any()

    def test_ignores_silence(self) -> None:
        assert probabilities(EnergyVad(), silence(1.5)).max() < VAD.threshold


class TestEndpointer:
    """The endpointer is what actually decides whether anything reaches the pipeline."""

    def feed(self, vad, samples: np.ndarray) -> list[bytes]:
        endpointer = Endpointer(vad, VAD, AUDIO)
        pcm = to_pcm(samples)
        step = AUDIO.frame_bytes
        out = []
        for offset in range(0, len(pcm) - step + 1, step):
            if (utterance := endpointer.push(pcm[offset:offset + step])) is not None:
                out.append(utterance)
        return out

    def test_speech_then_silence_yields_one_utterance(self, silero: SileroVad) -> None:
        silero.reset()
        utterances = self.feed(silero, np.concatenate([read_fixture(), silence(1.2)]))
        assert len(utterances) == 1, f"expected one utterance, got {len(utterances)}"
        seconds = len(utterances[0]) / 2 / 16000
        assert 1.0 < seconds < 4.0, f"utterance was {seconds:.1f}s"

    def test_pure_silence_yields_nothing(self, silero: SileroVad) -> None:
        silero.reset()
        assert self.feed(silero, silence(3.0)) == []

    def test_two_utterances_separated_by_a_pause(self, silero: SileroVad) -> None:
        silero.reset()
        speech = read_fixture()
        stream = np.concatenate([speech, silence(1.2), speech, silence(1.2)])
        assert len(self.feed(silero, stream)) == 2

    def test_a_brief_blip_is_ignored(self, silero: SileroVad) -> None:
        silero.reset()
        # Shorter than vad.min_speech_ms, so it must not become an utterance.
        blip = read_fixture()[:int(16000 * 0.2)]
        assert self.feed(silero, np.concatenate([blip, silence(1.2)])) == []

    def test_flush_emits_speech_cut_off_by_stopping(self, silero: SileroVad) -> None:
        silero.reset()
        endpointer = Endpointer(silero, VAD, AUDIO)
        pcm = to_pcm(read_fixture())
        step = AUDIO.frame_bytes
        for offset in range(0, len(pcm) - step + 1, step):
            endpointer.push(pcm[offset:offset + step])
        assert endpointer.flush() is not None, "mid-sentence audio should survive stopping"

    def test_preroll_keeps_audio_from_before_the_onset(self, silero: SileroVad) -> None:
        silero.reset()
        lead = 0.5
        utterances = self.feed(silero, np.concatenate([silence(lead), read_fixture(), silence(1.2)]))
        assert len(utterances) == 1
        # The utterance should start before the detected onset, but not include all the
        # leading silence.
        seconds = len(utterances[0]) / 2 / 16000
        assert seconds > 1.0
        assert seconds < 2.0 + lead + 1.0
