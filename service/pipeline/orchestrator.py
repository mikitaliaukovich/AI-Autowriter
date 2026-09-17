"""The pipeline: microphone -> utterance -> transcript -> ops -> document.

Two rules shape this module.

**Utterances are processed strictly one at a time.** Paragraph ids in the context
window are positions relative to the caret, so two batches in flight would invalidate
each other's ids. A queue absorbs the backlog while the user keeps talking.

**Capture never blocks on processing.** The audio loop only endpoints and enqueues;
transcription and generation happen in a worker. That is what lets ASR of the next
sentence overlap with the LLM writing the previous one.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import time
from typing import Any

from service import protocol
from service.asr import make_transcriber
from service.audio.capture import MicStream
from service.audio.hotkey import HotkeyError, HotkeyListener
from service.audio.vad import Endpointer, make_vad
from service.bridge import NotConnected, WordBridge
from service.config import Config
from service.llm import prompts
from service.llm.ollama_client import LlmError, OllamaClient
from service.llm.schema import LLM_RESPONSE_SCHEMA, OpsBatch, parse_ops
from service.pipeline import commands
from service.pipeline.finalize import Finalized, finalize_ops
from service.protocol import ApplyResult, DocumentContext

log = logging.getLogger(__name__)

# How many utterances may pile up before the oldest is dropped. Beyond this the user has
# out-talked the machine by so much that stale text is worse than no text.
QUEUE_LIMIT = 8

# Level meter cadence: every 5 frames is about 160 ms, smooth enough to read and far
# below the rate at which a WebSocket message costs anything.
LEVEL_EVERY_N_FRAMES = 5

SILENT_RMS = 1e-6


def _to_dbfs(rms: float) -> float:
    return 20 * math.log10(rms) if rms > SILENT_RMS else -100.0


async def _await_cancelled(task: asyncio.Task[Any]) -> None:
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def _guarded(what: str, coro: Any, *, timeout: float) -> None:
    """Await one shutdown step, giving up loudly rather than hanging the process."""
    try:
        await asyncio.wait_for(asyncio.shield(asyncio.ensure_future(coro)), timeout)
    except (asyncio.TimeoutError, TimeoutError):
        log.warning("shutdown: %s did not finish within %.0fs; continuing", what, timeout)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.warning("shutdown: %s failed (%s); continuing", what, exc)


class Orchestrator:
    def __init__(self, cfg: Config, bridge: WordBridge) -> None:
        self.cfg = cfg
        self.bridge = bridge
        self.llm = OllamaClient(cfg.llm)
        self.asr = make_transcriber(cfg.asr)

        self._listening = False
        self._queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()
        self._worker: asyncio.Task[None] | None = None
        self._audio_task: asyncio.Task[None] | None = None
        self._mic = MicStream(cfg.audio)
        self._endpointer: Endpointer | None = None
        self._hotkey: HotkeyListener | None = None
        self._hotkey_label = ""
        self._loop: asyncio.AbstractEventLoop | None = None
        self._warmup_task: asyncio.Task[None] | None = None
        self._llm_status = "starting"
        self._last_timing: dict[str, float] = {}
        self._busy = False

        bridge.on_command = self._on_pane_message
        bridge.on_connect = self.on_pane_attached

    # --- lifecycle --------------------------------------------------------------------

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._worker = asyncio.create_task(self._process_queue(), name="pipeline")
        self._start_hotkey()
        # Tracked, not fire-and-forget: warming loads the Whisper model in a worker
        # thread, and an untracked task carries on doing that after shutdown — which
        # left the interpreter waiting on it, and orphaned processes behind.
        self._warmup_task = asyncio.create_task(self._warm_models(), name="warmup")

    async def aclose(self) -> None:
        """Release everything, and never block shutdown on any one step.

        Each stage is individually guarded: a microphone that will not die, a hotkey
        thread that misses its quit message, or a model still loading must not be able
        to hold the whole process open.
        """
        await _guarded("stop listening", self.set_listening(False), timeout=5.0)

        for name, task in (("warmup", self._warmup_task),
                           ("pipeline", self._worker),
                           ("audio", self._audio_task)):
            if task is None or task.done():
                continue
            task.cancel()
            # A cancelled task awaiting `asyncio.to_thread` only unwinds once the thread
            # returns, so this is bounded rather than awaited indefinitely.
            await _guarded(f"stop {name}", _await_cancelled(task), timeout=5.0)

        if self._hotkey:
            await asyncio.to_thread(self._hotkey.stop)

        await _guarded("close LLM client", self.llm.aclose(), timeout=3.0)
        await _guarded("close ASR", self.asr.aclose(), timeout=3.0)
        log.info("pipeline shut down")

    def _start_hotkey(self) -> None:
        if not self.cfg.hotkey.enabled:
            return
        try:
            self._hotkey = HotkeyListener(self.cfg.hotkey.bindings, self._on_hotkey)
            self._hotkey_label = self._hotkey.start()
        except HotkeyError as exc:
            log.warning("hotkey unavailable: %s", exc)
            self._hotkey, self._hotkey_label = None, ""

    def _on_hotkey(self) -> None:
        """Called from the Win32 message thread — hop back onto the event loop."""
        if self._loop is not None:
            self._loop.call_soon_threadsafe(
                lambda: asyncio.create_task(self.set_listening(not self._listening))
            )

    async def _warm_models(self) -> None:
        ready, detail = await self.llm.health()
        self._llm_status = detail if ready else f"unavailable: {detail}"
        await self._push_state()
        if ready:
            await self.llm.warmup()
            self._llm_status = self.cfg.llm.model
        try:
            await self.asr.load()
        except Exception as exc:
            log.error("ASR unavailable: %s", exc)
        await self._push_state()

    # --- state ------------------------------------------------------------------------

    def state(self) -> dict[str, Any]:
        return {
            "listening": self._listening,
            "busy": self._busy,
            "queued": self._queue.qsize(),
            "hotkey": self._hotkey_label,
            "device": self.cfg.audio.device,
            "channel": self.cfg.audio.channel,
            "gainDb": self.cfg.audio.gain_db,
            "llmModel": self.cfg.llm.model,
            "llmStatus": self._llm_status,
            "contextBefore": self.cfg.context.before,
            "contextAfter": self.cfg.context.after,
            "timing": self._last_timing,
            **self.asr.state(),
        }

    async def _push_state(self) -> None:
        await self.bridge.broadcast(protocol.msg_state(**self.state()))

    async def _log(self, message: str, level: str = "info") -> None:
        log.info("pane[%s]: %s", level, message)
        await self.bridge.broadcast(protocol.msg_log(message, level))  # type: ignore[arg-type]

    async def on_pane_attached(self) -> None:
        await self._push_state()

    # --- inbound from the task pane ---------------------------------------------------

    async def _on_pane_message(self, kind: str, message: dict[str, Any]) -> None:
        if kind == "dictate":
            # Debug path: text typed into the pane, treated exactly as if spoken.
            text = str(message.get("text") or "").strip()
            if text:
                await self._enqueue(("text", text))
            return

        name = str(message.get("name") or "")
        if name == "start":
            await self.set_listening(True)
        elif name == "stop":
            await self.set_listening(False)
        elif name == "toggle":
            await self.set_listening(not self._listening)
        elif name == "undo":
            await self._apply_control("undo")
        elif name == "state":
            await self._push_state()
        elif name == "reloadConfig":
            await self._log("Restart the service to pick up config.toml changes.", "warn")

    # --- listening --------------------------------------------------------------------

    async def set_listening(self, on: bool) -> None:
        if on == self._listening:
            return
        if on:
            try:
                vad = make_vad(self.cfg.vad, self.cfg.audio, self.cfg.root / "models")
                self._endpointer = Endpointer(vad, self.cfg.vad, self.cfg.audio)
                await self._mic.start()
            except Exception as exc:
                log.error("could not start listening: %s", exc)
                await self._log(str(exc), "error")
                await self._push_state()
                return
            self._listening = True
            self._audio_task = asyncio.create_task(self._audio_loop(), name="audio")
            log.info("listening")
        else:
            self._listening = False
            if self._audio_task:
                self._audio_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._audio_task
                self._audio_task = None
            # Whatever was mid-sentence when the mic went off is still worth writing.
            if self._endpointer and (tail := self._endpointer.flush()):
                await self._enqueue(("audio", tail))
            await self._mic.stop()
            log.info("stopped listening")
        await self._push_state()

    async def _audio_loop(self) -> None:
        """Read frames, endpoint them, and keep the pane's level meter fed.

        The meter exists because a silent microphone is otherwise indistinguishable from
        a broken pipeline: nothing happens either way. With it, the failure is visible
        the moment the user starts talking.
        """
        assert self._endpointer is not None
        frames_since_push = 0
        peak = 0.0
        speech_seen = False
        heard_anything = False

        try:
            async for frame in self._mic.frames():
                utterance = self._endpointer.push(frame)

                peak = max(peak, self._endpointer.last_rms)
                speech_seen = speech_seen or self._endpointer.in_speech
                frames_since_push += 1
                if frames_since_push >= LEVEL_EVERY_N_FRAMES:
                    await self.bridge.broadcast(
                        protocol.msg_level(
                            _to_dbfs(self._endpointer.last_rms),
                            self._endpointer.in_speech,
                            _to_dbfs(peak),
                        )
                    )
                    frames_since_push = 0
                    peak = 0.0

                if utterance:
                    heard_anything = True
                    seconds = len(utterance) / 2 / self.cfg.audio.sample_rate
                    log.info("utterance: %.1f s", seconds)
                    await self._enqueue(("audio", utterance))

            if not heard_anything and not speech_seen:
                log.warning("capture ended without detecting any speech")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("audio loop failed")
            await self._log(f"Микрофон: {exc}", "error")
            self._listening = False
            await self._push_state()

    async def _enqueue(self, item: tuple[str, Any]) -> None:
        while self._queue.qsize() >= QUEUE_LIMIT:
            with contextlib.suppress(asyncio.QueueEmpty):
                self._queue.get_nowait()
                self._queue.task_done()
            await self._log("Очередь переполнена, пропущена реплика.", "warn")
        await self._queue.put(item)
        await self._push_state()

    # --- processing -------------------------------------------------------------------

    def _coalesce_audio(self, first: bytes) -> tuple[bytes, int]:
        """Join any audio already waiting behind this clip into one utterance.

        People pause mid-sentence. The endpointer cannot tell that pause from the end of
        a thought, so a single sentence often arrives as two clips — and transcribing
        them separately gives Whisper half a phrase each and asks the model to stitch
        the halves together in the document, which is where edits went wrong.

        Anything already queued was spoken while we were busy, so it is by definition
        continuous with this clip. Merging costs nothing (one transcription instead of
        two) and hands Whisper the complete phrase.
        """
        chunks = [first]
        total = len(first)
        limit = self.cfg.vad.max_utterance_ms * self.cfg.audio.sample_rate * 2 // 1000

        while total < limit:
            try:
                kind, payload = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if kind != "audio":
                # Only audio coalesces. Typed input goes back on the queue (at the end,
                # which is harmless: it only comes from the pane's debug box).
                self._queue.put_nowait((kind, payload))
                self._queue.task_done()
                break
            chunks.append(payload)
            total += len(payload)
            self._queue.task_done()

        return b"".join(chunks), len(chunks)

    async def _process_queue(self) -> None:
        while True:
            kind, payload = await self._queue.get()
            self._busy = True
            await self._push_state()
            try:
                if kind == "audio":
                    merged, parts = self._coalesce_audio(payload)
                    if parts > 1:
                        seconds = len(merged) / 2 / self.cfg.audio.sample_rate
                        log.info("merged %d queued clips into %.1f s", parts, seconds)
                    await self._handle_audio(merged)
                else:
                    await self._handle_text(str(payload))
            except NotConnected:
                await self._log("Панель Word не подключена — реплика пропущена.", "warn")
            except Exception as exc:
                log.exception("pipeline error")
                await self._log(f"Ошибка обработки: {exc}", "error")
            finally:
                self._busy = False
                self._queue.task_done()
                await self._push_state()

    def _asr_prompt(self) -> str:
        """Seed Whisper with recent text and project names.

        This is the highest-leverage quality knob in the whole system: without it,
        character names drift spelling from one paragraph to the next.
        """
        pieces: list[str] = []
        names = self.cfg.project.all_names
        if names:
            pieces.append(", ".join(names) + ".")
        context = self.bridge.last_context
        if context:
            tail = " ".join(p.text for p in context.paragraphs if p.text).strip()
            if tail:
                pieces.append(tail[-self.cfg.asr.prompt_context_chars:])
        return " ".join(pieces).strip()

    async def _handle_audio(self, pcm: bytes) -> None:
        transcript = await self.asr.transcribe(pcm, prompt=self._asr_prompt())
        self._last_timing = {
            "audioS": round(transcript.duration_s, 2),
            "asrMs": round(transcript.latency_ms),
            "rtf": round(transcript.realtime_factor, 1),
        }
        if transcript.dropped:
            await self.bridge.try_send(
                protocol.msg_transcript(transcript.text, final=True, kind="dropped")
            )
            await self._log(f"Пропущено ({transcript.dropped}): {transcript.text[:60]}", "warn")
            return
        await self.bridge.try_send(protocol.msg_transcript(transcript.text))
        await self._handle_text(transcript.text)

    async def _handle_text(self, text: str) -> None:
        parsed = commands.parse(text)
        log.info("[%s] %s", parsed.kind, text[:120])

        if parsed.kind == "control":
            if parsed.control:
                await self._apply_control(parsed.control)
            return

        await self._run_llm(parsed.text, kind=parsed.kind)

    async def _apply_control(self, control: str) -> None:
        """Deterministic actions that need no model call."""
        if control == "stop":
            await self.set_listening(False)
            return

        ops: list[dict[str, Any]]
        if control == "undo":
            ops = [{"op": "revert", "count": 1}]
        elif control == "new_paragraph":
            ops = [{"op": "insert_paragraphs_after", "id": "P0",
                    "paragraphs": [{"text": "", "style": "normal"}]}]
        elif control == "new_chapter":
            ops = [{"op": "insert_paragraphs_after", "id": "P0",
                    "paragraphs": [{"text": "", "style": "heading1"}]}]
        else:
            return

        result = await self.bridge.apply(ops, meta={"source": "control", "control": control})
        if not result.ok:
            await self._log(f"Не удалось выполнить «{control}»: {result.error}", "warn")

    async def _run_llm(self, utterance: str, *, kind: str) -> None:
        """Turn one utterance into document edits, never giving up silently.

        Dictated words are expensive to lose — the author has already moved on and will
        not notice a sentence went missing until much later. So both failure modes get a
        second attempt with the model told exactly what went wrong: a fragment it could
        not place, or a paragraph that changed underneath it.
        """
        context = await self.bridge.read_context()
        started = time.perf_counter()

        batch, final = await self._generate(context, utterance, kind)
        result = None

        if final.ops:
            result = await self.bridge.apply(
                final.ops, meta={"mode": batch.mode, "note": batch.note}
            )

        correction = self._correction_for(final, result)
        if correction:
            await self._log("Не получилось с первого раза, переспрашиваю модель…", "warn")
            context = await self.bridge.read_context()
            batch, final = await self._generate(context, utterance, kind, correction=correction)
            if final.ops:
                result = await self.bridge.apply(
                    final.ops, meta={"mode": batch.mode, "retry": True}
                )

        self._last_timing = {
            **self._last_timing,
            "llmMs": round((time.perf_counter() - started) * 1000),
        }
        await self.bridge.try_send(protocol.msg_timing(**self._last_timing))

        if result is None:
            detail = "; ".join(final.problems) or batch.note or "нет изменений"
            await self._log(f"Без правок: {detail}", "warn" if final.problems else "info")
        elif not result.ok:
            detail = result.error or ", ".join(c.reason for c in result.conflicts)
            await self._log(f"Правка не применена: {detail}", "error")
            # The words are gone from the document but not from the log, so the author
            # can at least see what was heard and repeat it.
            await self._log(f"Не записано: «{utterance[:120]}»", "error")

    def _correction_for(self, final: Finalized, result: ApplyResult | None) -> str:
        """What to tell the model on a second attempt, or "" if there is nothing to retry."""
        if final.problems:
            return (
                "твоя прошлая попытка не удалась — фрагмент из поля find отсутствует в абзаце "
                f"({final.problems[0]}). Не используй replace_in_paragraph. "
                "Верни replace_paragraph с полным новым текстом абзаца или append_to_paragraph."
            )
        if result is not None and not result.ok and result.conflicts:
            return (
                "документ изменился с момента прошлой попытки. "
                "Пересчитай правку по текущему окну контекста."
            )
        return ""

    async def _generate(
        self, context: DocumentContext, utterance: str, kind: str, correction: str = ""
    ) -> tuple[OpsBatch, Finalized]:
        messages = prompts.build_messages(
            context,
            utterance,
            kind=kind,
            project=self.cfg.project,
            max_chars=self.cfg.context.max_chars,
            correction=correction,
        )
        try:
            completion = await self.llm.complete_json(messages, LLM_RESPONSE_SCHEMA)
        except LlmError as exc:
            await self._log(f"Модель недоступна: {exc}", "error")
            return OpsBatch(), Finalized()

        batch = parse_ops(completion.payload, context.ids)
        final = finalize_ops(batch, context, self.cfg.typography)
        log.debug(
            "llm %.0f ms, %d ops, %d problems: %s",
            completion.latency_ms, len(final.ops), len(final.problems), batch.note,
        )
        for problem in final.problems:
            log.info("unresolved: %s", problem)
        return batch, final
