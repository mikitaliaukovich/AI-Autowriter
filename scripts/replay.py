"""Replay an audio file through the **live** pipeline, with Word stubbed out.

`check_audio.py` measures the microphone. This exercises everything after it: the real
orchestrator, the real endpointer, the real ASR and LLM, the real queue and worker
tasks — the only substitutions are a file in place of the microphone and a fake task
pane in place of Word. So if dictation produces nothing while the level meter is
healthy, this is what narrows down which stage swallows it.

    python scripts/replay.py clip.wav
    python scripts/replay.py clip.wav --language en --context "Мэри молчала. / |"
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import dataclasses
import json
import logging
import pathlib
import sys
from collections.abc import AsyncIterator

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from service import config as config_module  # noqa: E402
from service.audio.decode import decode_to_pcm  # noqa: E402
from service.bridge import WordBridge  # noqa: E402
from service.console import enable_utf8  # noqa: E402
from service.pipeline.orchestrator import Orchestrator  # noqa: E402
from service.protocol import text_hash  # noqa: E402
from service.textspec import build_context  # noqa: E402


class FileStream:
    """Stands in for MicStream, yielding frames from a file in real time-ish order."""

    def __init__(self, pcm: bytes, frame_bytes: int, trailing_silence_frames: int) -> None:
        self.pcm = pcm
        self.frame_bytes = frame_bytes
        self.trailing = trailing_silence_frames
        self.frames_yielded = 0
        self.running = False

    async def start(self) -> None:
        self.running = True

    async def stop(self) -> None:
        self.running = False

    async def frames(self) -> AsyncIterator[bytes]:
        self.running = True
        for offset in range(0, len(self.pcm) - self.frame_bytes + 1, self.frame_bytes):
            self.frames_yielded += 1
            yield self.pcm[offset:offset + self.frame_bytes]
            await asyncio.sleep(0)
        # Silence so the endpointer closes the final utterance, exactly as a real pause would.
        for _ in range(self.trailing):
            self.frames_yielded += 1
            yield b"\x00" * self.frame_bytes
            await asyncio.sleep(0)
        self.running = False


class StubPane:
    """A fake task pane backed by a real in-memory document.

    It must actually apply the edits, not just record them. A stub that always replays
    the same context cannot catch sequencing bugs — and sequencing is exactly where
    dictation goes wrong, because each utterance is written against the document the
    previous one left behind.

    It mirrors the task pane's contract: verify every ``expect`` hash before touching
    anything, then move the caret to the end of what was written.
    """

    def __init__(self, context_spec: str) -> None:
        seed = build_context(context_spec)
        self.paragraphs: list[dict] = [
            {"text": p.text, "style": p.style} for p in seed.paragraphs
        ]
        anchor = next((i for i, p in enumerate(seed.paragraphs) if p.id == "P0"), 0)
        self.caret = anchor
        self.applied: list[dict] = []
        self.context_reads = 0
        self.rejected: list[str] = []

    # --- document ------------------------------------------------------------------

    def window(self, before: int = 6, after: int = 2) -> list[dict]:
        start = max(0, self.caret - before)
        end = min(len(self.paragraphs), self.caret + after + 1)
        out = []
        for index in range(start, end):
            offset = index - self.caret
            paragraph = self.paragraphs[index]
            entry = {
                "id": "P0" if offset == 0 else f"P{offset:+d}",
                "text": paragraph["text"],
                "style": paragraph["style"],
                "hash": text_hash(paragraph["text"]),
                "empty": not paragraph["text"],
            }
            if offset == 0:
                entry["caret"] = len(paragraph["text"])
            out.append(entry)
        return out

    def _index_of(self, pid: str) -> int | None:
        offset = 0 if pid == "P0" else int(pid[1:])
        index = self.caret + offset
        return index if 0 <= index < len(self.paragraphs) else None

    def _apply(self, ops: list[dict]) -> dict:
        # Verify every hash first: the batch applies wholly or not at all.
        for position, op in enumerate(ops):
            pid = op.get("id")
            if not pid:
                continue
            index = self._index_of(pid)
            if index is None:
                return {"ok": False, "applied": 0, "error": "",
                        "conflicts": [{"index": position, "id": pid, "reason": "абзац вне окна"}]}
            if op.get("expect") and text_hash(self.paragraphs[index]["text"]) != op["expect"]:
                return {"ok": False, "applied": 0, "error": "",
                        "conflicts": [{"index": position, "id": pid,
                                       "reason": "абзац изменился после чтения контекста"}]}

        applied = 0
        for op in ops:
            name = op["op"]
            if name in ("noop", "revert"):
                continue
            index = self._index_of(op["id"])
            assert index is not None
            if name == "append_to_paragraph":
                self.paragraphs[index]["text"] += op["text"]
                self.caret = index
            elif name == "replace_paragraph":
                self.paragraphs[index]["text"] = op["text"]
                if op.get("style"):
                    self.paragraphs[index]["style"] = op["style"]
                self.caret = index
            elif name == "insert_paragraphs_after":
                for offset, new in enumerate(op["paragraphs"], start=1):
                    self.paragraphs.insert(
                        index + offset, {"text": new["text"], "style": new.get("style", "normal")}
                    )
                self.caret = index + len(op["paragraphs"])
            elif name == "delete_paragraph":
                self.paragraphs.pop(index)
                self.caret = max(0, index - 1)
            elif name == "set_style":
                self.paragraphs[index]["style"] = op["style"]
            else:
                return {"ok": False, "applied": applied, "conflicts": [],
                        "error": f"неизвестная операция {name}"}
            applied += 1
        return {"ok": True, "applied": applied, "conflicts": [], "error": ""}

    # --- protocol ------------------------------------------------------------------

    async def send_json(self, message: dict) -> None:
        kind = message.get("type")
        if kind == "requestContext":
            self.context_reads += 1
            await self._reply({
                "type": "context",
                "reqId": message["reqId"],
                "paragraphs": self.window(),
                "atEndOfParagraph": True,
            })
        elif kind == "apply":
            result = self._apply(message["ops"])
            if result["ok"]:
                self.applied.extend(message["ops"])
            else:
                self.rejected.append(
                    result["error"] or "; ".join(c["reason"] for c in result["conflicts"])
                )
            await self._reply({
                "type": "applyResult",
                "reqId": message["reqId"],
                "result": result,
            })

    async def close(self) -> None:
        pass

    # Filled in by main() so replies can be routed back into the bridge.
    _bridge: WordBridge | None = None

    async def _reply(self, message: dict) -> None:
        assert self._bridge is not None
        await self._bridge.handle(self, message)


async def main() -> int:
    enable_utf8()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("audio", help="audio file to replay (any format ffmpeg reads)")
    parser.add_argument("--context", default="|", help='window spec: paragraphs "/"-separated, caret "|"')
    parser.add_argument("--language", default="", help="override the ASR language")
    parser.add_argument("--verbose", action="store_true", help="show service log output")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="    %(levelname)-7s %(name)s  %(message)s",
        stream=sys.stdout,
    )
    for noisy in ("httpx", "httpcore", "huggingface_hub", "filelock", "urllib3", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    cfg = config_module.load()
    if args.language:
        cfg = dataclasses.replace(cfg, asr=dataclasses.replace(cfg.asr, language=args.language))

    pcm = decode_to_pcm(args.audio, cfg.audio.ffmpeg, cfg.audio.sample_rate)
    seconds = len(pcm) / 2 / cfg.audio.sample_rate
    print(f"audio      : {seconds:.1f} s from {args.audio}")
    print(f"vad        : threshold={cfg.vad.threshold} silence={cfg.vad.silence_ms}ms "
          f"min_speech={cfg.vad.min_speech_ms}ms")
    print(f"asr        : {cfg.asr.model} ({cfg.asr.language})")
    print(f"llm        : {cfg.llm.model}\n")

    bridge = WordBridge()
    pane = StubPane(args.context)
    pane._bridge = bridge
    await bridge.attach(pane)
    await bridge.handle(pane, {"type": "hello", "client": "word",
                               "wordApi": {"1.1": True, "1.3": True}})

    orchestrator = Orchestrator(cfg, bridge)
    # The hotkey is irrelevant here and would collide with a running service.
    orchestrator.cfg = dataclasses.replace(cfg, hotkey=dataclasses.replace(cfg.hotkey, enabled=False))

    trailing = max(1, int((cfg.vad.silence_ms + 400) / cfg.audio.frame_ms))
    stream = FileStream(pcm, cfg.audio.frame_bytes, trailing)

    await orchestrator.start()
    orchestrator._mic = stream          # noqa: SLF001 — deliberate injection point
    try:
        await orchestrator.set_listening(True)
        # Wait for capture to drain, then for the queue to finish processing.
        while stream.running or orchestrator._queue.qsize() or orchestrator._busy:  # noqa: SLF001
            await asyncio.sleep(0.2)
        await asyncio.sleep(0.5)
        await orchestrator.set_listening(False)
        for _ in range(60):
            if not orchestrator._queue.qsize() and not orchestrator._busy:  # noqa: SLF001
                break
            await asyncio.sleep(0.5)
    finally:
        with contextlib.suppress(Exception):
            await orchestrator.aclose()

    print("\n" + "=" * 68)
    print(f"frames fed to the endpointer : {stream.frames_yielded}")
    print(f"context reads by the service : {pane.context_reads}")
    print(f"operations applied to Word   : {len(pane.applied)}")
    for op in pane.applied:
        print("   " + json.dumps(op, ensure_ascii=False))
    if pane.rejected:
        print(f"batches REJECTED             : {len(pane.rejected)}")
        for reason in pane.rejected:
            print(f"   {reason}")

    print("\nresulting document:")
    for index, paragraph in enumerate(pane.paragraphs):
        marker = ">" if index == pane.caret else " "
        style = "" if paragraph["style"] == "normal" else f"[{paragraph['style']}] "
        print(f"  {marker} {style}{paragraph['text'] or '(пусто)'}")

    if not pane.applied:
        print("\nNothing reached the document. The log above shows which stage stopped:")
        print("  no 'utterance: N.N s' line  -> the endpointer never closed an utterance (VAD)")
        print("  'suppressed transcript'     -> the hallucination filter dropped it")
        print("  'Без правок'                -> the model chose to do nothing")
    return 0 if pane.applied else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
