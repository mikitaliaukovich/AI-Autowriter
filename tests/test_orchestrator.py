"""Pipeline behaviour that protects dictated words from being lost.

Two mechanisms are covered:

* **Coalescing.** A pause mid-sentence splits one thought into two clips. Anything
  already queued was spoken while the pipeline was busy, so it is continuous with the
  clip in hand and gets merged before transcription.
* **Retrying with a correction.** When an edit cannot be placed, the model is re-asked
  with the reason rather than the sentence being dropped.
"""
from __future__ import annotations

import asyncio

import pytest

from service import config as config_module
from service.bridge import WordBridge
from service.pipeline.finalize import Finalized
from service.pipeline.orchestrator import Orchestrator
from service.protocol import ApplyConflict, ApplyResult


@pytest.fixture
def orchestrator() -> Orchestrator:
    return Orchestrator(config_module.load(), WordBridge())


def clip(seconds: float, rate: int = 16000) -> bytes:
    return b"\x01\x00" * int(rate * seconds)


class TestCoalescing:
    def test_a_lone_clip_passes_through_unchanged(self, orchestrator: Orchestrator) -> None:
        audio = clip(2.0)
        merged, parts = orchestrator._coalesce_audio(audio)
        assert merged == audio
        assert parts == 1

    def test_queued_clips_are_merged(self, orchestrator: Orchestrator) -> None:
        first, second, third = clip(1.0), clip(1.5), clip(0.5)
        orchestrator._queue.put_nowait(("audio", second))
        orchestrator._queue.put_nowait(("audio", third))

        merged, parts = orchestrator._coalesce_audio(first)
        assert parts == 3
        assert merged == first + second + third
        assert orchestrator._queue.qsize() == 0

    def test_merging_stops_at_the_maximum_utterance_length(self, orchestrator: Orchestrator) -> None:
        limit_s = orchestrator.cfg.vad.max_utterance_ms / 1000
        for _ in range(6):
            orchestrator._queue.put_nowait(("audio", clip(limit_s / 2)))

        merged, parts = orchestrator._coalesce_audio(clip(limit_s / 2))
        seconds = len(merged) / 2 / orchestrator.cfg.audio.sample_rate
        assert seconds <= limit_s + limit_s / 2, f"merged {seconds:.1f}s"
        assert parts < 7, "must not swallow the whole queue"
        assert orchestrator._queue.qsize() > 0, "the rest stays queued"

    def test_typed_input_is_not_merged_and_is_not_lost(self, orchestrator: Orchestrator) -> None:
        orchestrator._queue.put_nowait(("text", "Ассистент, отмени"))
        orchestrator._queue.put_nowait(("audio", clip(1.0)))

        merged, parts = orchestrator._coalesce_audio(clip(1.0))
        assert parts == 1, "must stop at the non-audio item"
        assert orchestrator._queue.qsize() == 2, "both items remain queued"

    def test_queue_accounting_stays_consistent(self, orchestrator: Orchestrator) -> None:
        # join() must not hang afterwards, which it would if task_done() were unbalanced.
        for _ in range(3):
            orchestrator._queue.put_nowait(("audio", clip(0.5)))
        orchestrator._coalesce_audio(clip(0.5))

        async def drained() -> bool:
            await asyncio.wait_for(orchestrator._queue.join(), timeout=1.0)
            return True

        assert asyncio.run(drained())


class TestCorrectionForRetry:
    def test_an_unplaceable_fragment_asks_for_a_whole_paragraph(self, orchestrator: Orchestrator) -> None:
        final = Finalized(ops=[], problems=["фрагмент «абв» не найден в абзаце P0"])
        correction = orchestrator._correction_for(final, None)
        assert "replace_in_paragraph" in correction
        assert "replace_paragraph" in correction

    def test_a_changed_document_asks_for_a_recompute(self, orchestrator: Orchestrator) -> None:
        result = ApplyResult(ok=False, conflicts=[ApplyConflict(index=0, id="P0", reason="изменился")])
        correction = orchestrator._correction_for(Finalized(ops=[{"op": "noop"}]), result)
        assert "документ изменился" in correction

    def test_success_needs_no_retry(self, orchestrator: Orchestrator) -> None:
        result = ApplyResult(ok=True, applied=1)
        assert orchestrator._correction_for(Finalized(ops=[{"op": "noop"}]), result) == ""

    def test_a_clean_no_op_needs_no_retry(self, orchestrator: Orchestrator) -> None:
        assert orchestrator._correction_for(Finalized(), None) == ""
