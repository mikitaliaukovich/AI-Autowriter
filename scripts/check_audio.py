"""Diagnose the microphone path, from raw levels to what Whisper actually hears.

Run this whenever dictation "does nothing". It records a few seconds, then answers the
four questions that separate the possible causes:

1. Is any audio arriving at all, and on which channel?  (per-channel level)
2. Does the voice detector consider it speech?           (VAD speech ratio)
3. Would an utterance ever be emitted?                   (endpointer simulation)
4. Does Whisper understand it?                           (transcription)

It ends with the exact config.toml lines to use.

    python scripts/check_audio.py
    python scripts/check_audio.py --seconds 8 --no-asr
"""
from __future__ import annotations

import argparse
import asyncio
import dataclasses
import math
import pathlib
import subprocess
import sys
import wave

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402

from service import config as config_module  # noqa: E402
from service.asr import make_transcriber  # noqa: E402
from service.audio.capture import list_devices  # noqa: E402
from service.audio.vad import SILERO_WINDOW, Endpointer, make_vad  # noqa: E402
from service.console import enable_utf8  # noqa: E402

SILENT_DBFS = -60.0     # below this a channel is, for practical purposes, dead
QUIET_DBFS = -32.0      # below this Whisper and the VAD start to struggle
CHANNEL_DOMINANCE_DB = 12.0  # one channel this much louder means the mic is on it


def record_stereo(cfg, seconds: float) -> np.ndarray:
    """Record without any downmix. Returns an (n, 2) float32 array in [-1, 1]."""
    args = [
        cfg.audio.ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin",
        "-f", "dshow", "-audio_buffer_size", "50",
        "-i", f"audio={cfg.audio.device}",
        "-t", str(seconds),
        "-ar", str(cfg.audio.sample_rate),
        "-ac", "2",                     # keep both channels so they can be compared
        "-f", "s16le", "-",
    ]
    result = subprocess.run(args, capture_output=True, timeout=seconds + 30)
    if result.returncode != 0 or not result.stdout:
        detail = result.stderr.decode("utf-8", "replace").strip().splitlines()
        hint = detail[-1] if detail else f"exit code {result.returncode}"
        available = "\n  ".join(list_devices(cfg.audio.ffmpeg)) or "(none found)"
        raise SystemExit(
            f"Could not record from '{cfg.audio.device}': {hint}\n\n"
            f"Available inputs:\n  {available}\n\n"
            "Set audio.device in config.toml to one of these, exactly as printed."
        )
    samples = np.frombuffer(result.stdout, dtype=np.int16).astype(np.float32) / 32768.0
    return samples.reshape(-1, 2)


def dbfs(samples: np.ndarray) -> float:
    if samples.size == 0:
        return -math.inf
    rms = float(np.sqrt(np.mean(np.square(samples))))
    return 20 * math.log10(rms) if rms > 1e-9 else -math.inf


def bar(value_db: float, width: int = 28) -> str:
    """A level meter from -60 dBFS to 0."""
    if not math.isfinite(value_db):
        return "·" * width
    filled = max(0, min(width, round((value_db + 60) / 60 * width)))
    return "█" * filled + "·" * (width - filled)


def describe_level(value_db: float) -> str:
    if not math.isfinite(value_db) or value_db < SILENT_DBFS:
        return "silent"
    if value_db < QUIET_DBFS:
        return "very quiet"
    if value_db > -3:
        return "clipping risk"
    return "good"


def analyse(name: str, mono: np.ndarray, cfg) -> dict:
    """Run the VAD and the endpointer over one candidate mono signal."""
    vad = make_vad(cfg.vad, cfg.audio, cfg.root / "models")
    frames = len(mono) // SILERO_WINDOW
    speech = 0
    for i in range(frames):
        window = mono[i * SILERO_WINDOW:(i + 1) * SILERO_WINDOW]
        if vad.probability(window) >= cfg.vad.threshold:
            speech += 1

    # Replay through the real endpointer to see whether an utterance would be emitted.
    endpointer = Endpointer(make_vad(cfg.vad, cfg.audio, cfg.root / "models"), cfg.vad, cfg.audio)
    pcm = (np.clip(mono, -1, 1) * 32767).astype(np.int16).tobytes()
    step = cfg.audio.frame_bytes
    utterances = []
    for offset in range(0, len(pcm) - step + 1, step):
        if (utterance := endpointer.push(pcm[offset:offset + step])) is not None:
            utterances.append(utterance)
    if (tail := endpointer.flush()) is not None:
        utterances.append(tail)

    return {
        "name": name,
        "dbfs": dbfs(mono),
        "speech_ratio": speech / frames if frames else 0.0,
        "utterances": utterances,
        "mono": mono,
    }


async def main() -> int:
    enable_utf8()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--seconds", type=float, default=6.0, help="how long to record")
    parser.add_argument("--no-asr", action="store_true", help="skip transcription")
    parser.add_argument("--save", default="", help="also write the recording to this WAV file")
    args = parser.parse_args()

    cfg = config_module.load()
    print(f"Device: {cfg.audio.device}")
    print(f"Config: channel={cfg.audio.channel!r}  gain_db={cfg.audio.gain_db}\n")
    print(f"Speak normally for {args.seconds:.0f} seconds — say a full sentence, in Russian.")
    for count in (3, 2, 1):
        print(f"  {count}...", end="\r", flush=True)
        await asyncio.sleep(1)
    print("  RECORDING     ")

    stereo = record_stereo(cfg, args.seconds)
    print(f"  done ({len(stereo) / cfg.audio.sample_rate:.1f} s)\n")

    if args.save:
        with wave.open(args.save, "wb") as handle:
            handle.setnchannels(2)
            handle.setsampwidth(2)
            handle.setframerate(cfg.audio.sample_rate)
            handle.writeframes((np.clip(stereo, -1, 1) * 32767).astype(np.int16).tobytes())
        print(f"Saved {args.save}\n")

    candidates = {
        "left":  stereo[:, 0],
        "right": stereo[:, 1],
        "mix":   stereo.mean(axis=1),
    }
    identical = bool(np.allclose(stereo[:, 0], stereo[:, 1]))

    print(f"{'channel':<8} {'level':>8}  {'meter':<28} {'speech':>7}  {'utterances':>10}")
    print("-" * 70)
    results = {}
    for name, mono in candidates.items():
        results[name] = analyse(name, mono, cfg)
        r = results[name]
        level = f"{r['dbfs']:.1f}" if math.isfinite(r["dbfs"]) else "  -inf"
        print(
            f"{name:<8} {level:>7}d  {bar(r['dbfs']):<28}"
            f" {r['speech_ratio'] * 100:>6.0f}%  {len(r['utterances']):>10}"
        )
    print()

    # --- verdict -------------------------------------------------------------------
    # Channels are compared *relatively*. Absolute level says little here: a mic's noise
    # floor can sit below -60 dBFS while still being the only live channel, so an
    # absolute "is it silent" test would call both channels dead and pick arbitrarily.
    speech_detected = any(r["speech_ratio"] > 0.02 for r in results.values())
    left_db, right_db = results["left"]["dbfs"], results["right"]["dbfs"]

    def finite(value: float) -> float:
        return value if math.isfinite(value) else -140.0

    difference = finite(left_db) - finite(right_db)

    if identical:
        print("Both channels are identical — a mono input, so the channel choice does not matter.")
        best = "mix"
    elif abs(difference) >= CHANNEL_DOMINANCE_DB:
        best = "left" if difference > 0 else "right"
        other = "right" if best == "left" else "left"
        print(
            f"The {best} channel is {abs(difference):.0f} dB above the {other} channel, so the\n"
            f'microphone is on {best}. A "mix" downmix averages it with a near-silent\n'
            f'channel and throws away about 6 dB — set channel = "{best}".'
        )
    elif speech_detected:
        best = max(("left", "right", "mix"), key=lambda n: results[n]["speech_ratio"])
        print(f"Both channels carry comparable signal; '{best}' scored best for speech detection.")
    else:
        best = "mix"
        print("Both channels are at a similar level and no speech was detected.")

    chosen = results[best]

    if not speech_detected:
        print(
            "\nNO SPEECH WAS DETECTED in this recording.\n"
            "  - If you did not speak, run it again and say a full sentence.\n"
            "  - If you did speak, the sound is not reaching this machine: check that the\n"
            "    interface's input gain is up, that the channel named above is the one the\n"
            "    mic is plugged into, and that Windows Settings > Privacy > Microphone\n"
            "    allows desktop apps."
        )
        print(f"\nLevels seen: left {left_db:.1f} dBFS, right {right_db:.1f} dBFS.")
        gain = 0.0
    else:
        print(f"\nBest candidate: {best}  ({chosen['dbfs']:.1f} dBFS, {describe_level(chosen['dbfs'])})")
        gain = 0.0
        if math.isfinite(chosen["dbfs"]) and chosen["dbfs"] < QUIET_DBFS:
            gain = float(round(min(24.0, -12.0 - chosen["dbfs"])))
            print(f"That is quiet. Suggested gain_db = {gain:.0f}, or raise the interface's input gain.")

        if not chosen["utterances"]:
            print(
                "\nSpeech was detected but no complete utterance was produced. Try lowering\n"
                f"vad.threshold (currently {cfg.vad.threshold}) or vad.min_speech_ms "
                f"(currently {cfg.vad.min_speech_ms})."
            )
        else:
            seconds = sum(len(u) for u in chosen["utterances"]) / 2 / cfg.audio.sample_rate
            print(f"Endpointer produced {len(chosen['utterances'])} utterance(s), {seconds:.1f} s of speech.")

    # --- what Whisper hears ---------------------------------------------------------
    if not args.no_asr and chosen["utterances"]:
        print("\nTranscribing what the pipeline would actually send to Whisper...")
        transcriber = make_transcriber(cfg.asr)
        for index, utterance in enumerate(chosen["utterances"], 1):
            transcript = await transcriber.transcribe(utterance)
            status = f"SUPPRESSED ({transcript.dropped})" if transcript.dropped else "ok"
            print(f"  [{index}] {status}: {transcript.text!r}")
        await transcriber.aclose()

    print("\n" + "=" * 70)
    print("Put this in config.toml under [audio]:")
    print(f'    channel = "{best}"')
    print(f"    gain_db = {gain}")
    print("Then restart the service.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
