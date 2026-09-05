"""Run one utterance through the LLM + typography stages and print the ops.

No Word and no microphone involved, so this is the fastest way to iterate on the prompt
or compare models.

    python scripts/dictate.py "Диалог Почему ты это сделал Мэри сказала гневно точка"
    python scripts/dictate.py --context "Он вышел на улицу и|" --model qwen3:14b "..."
    python scripts/dictate.py --audio sample.wav --context "Мэри молчала. / |"

Context paragraphs are separated by "/", and "|" marks the caret.
"""
from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from service import config as config_module  # noqa: E402
from service.asr import make_transcriber  # noqa: E402
from service.audio.decode import decode_to_pcm  # noqa: E402
from service.console import enable_utf8  # noqa: E402
from service.llm import prompts  # noqa: E402
from service.llm.ollama_client import OllamaClient  # noqa: E402
from service.llm.schema import LLM_RESPONSE_SCHEMA, parse_ops  # noqa: E402
from service.pipeline import commands  # noqa: E402
from service.pipeline.finalize import finalize_ops  # noqa: E402
from service.textspec import build_context  # noqa: E402


def render(ops: list[dict]) -> str:
    lines = []
    for op in ops:
        name = op["op"]
        target = f" {op['id']}" if op.get("id") else ""
        if name == "insert_paragraphs_after":
            lines.append(f"{name}{target}:")
            for p in op["paragraphs"]:
                style = "" if p["style"] == "normal" else f"  [{p['style']}]"
                lines.append(f"    {p['text']}{style}")
        elif name in ("append_to_paragraph", "replace_paragraph", "insert_at_caret"):
            lines.append(f"{name}{target}: {op['text']!r}")
        elif name == "replace_in_paragraph":
            lines.append(f"{name}{target}: {op['find']!r} -> {op['replace']!r}")
        else:
            lines.append(f"{name}{target}: {json.dumps(op, ensure_ascii=False)}")
    return "\n".join(lines) or "(нет операций)"


async def main() -> int:
    enable_utf8()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "utterance", nargs="?", default="",
        help="what the author said, as Whisper would transcribe it (omit when using --audio)",
    )
    parser.add_argument("--audio", default="", help="transcribe this audio file instead of taking text")
    parser.add_argument("--language", default="", help="override the ASR language, e.g. en")
    parser.add_argument("--context", default="", help='paragraphs separated by "/", caret marked with "|"')
    parser.add_argument("--model", default="", help="override the configured model")
    parser.add_argument("--raw", action="store_true", help="also print the model's raw JSON")
    args = parser.parse_args()

    cfg = config_module.load()
    llm_cfg = dataclasses.replace(cfg.llm, model=args.model) if args.model else cfg.llm

    utterance = args.utterance
    if args.audio:
        asr_cfg = dataclasses.replace(cfg.asr, language=args.language) if args.language else cfg.asr
        transcriber = make_transcriber(asr_cfg)
        pcm = decode_to_pcm(args.audio, cfg.audio.ffmpeg, cfg.audio.sample_rate)
        print(f"audio: {len(pcm) / 2 / cfg.audio.sample_rate:.1f} s from {args.audio}")
        transcript = await transcriber.transcribe(pcm)
        await transcriber.aclose()
        print(
            f"asr:   {transcript.latency_ms:.0f} ms"
            f"  (x{transcript.realtime_factor:.1f} realtime)"
            f"  no_speech={transcript.no_speech_prob:.2f} logprob={transcript.avg_logprob:.2f}"
        )
        if transcript.dropped:
            print(f"asr:   SUPPRESSED — {transcript.dropped}")
            print(f"       raw text was {transcript.text!r}")
            return 0
        utterance = transcript.text
        print(f"heard: {utterance!r}\n")
    elif not utterance:
        parser.error("give an utterance, or use --audio FILE")

    context = build_context(args.context)
    parsed = commands.parse(utterance)
    print(f"tier-1: {parsed.kind}" + (f" ({parsed.control})" if parsed.control else ""))
    if parsed.kind == "control":
        print("Handled without the model.")
        return 0

    print(f"context:\n{context.render()}\n")

    client = OllamaClient(llm_cfg)
    try:
        ready, detail = await client.health()
        if not ready:
            print(f"LLM unavailable: {detail}", file=sys.stderr)
            return 1

        messages = prompts.build_messages(
            context, parsed.text, kind=parsed.kind, project=cfg.project, max_chars=cfg.context.max_chars
        )
        started = time.perf_counter()
        completion = await client.complete_json(messages, LLM_RESPONSE_SCHEMA)
        elapsed = (time.perf_counter() - started) * 1000
    finally:
        await client.aclose()

    batch = parse_ops(completion.payload, context.ids)
    ops = finalize_ops(batch, context, cfg.typography)

    if args.raw:
        print("raw:", completion.raw, "\n")
    print(f"mode={batch.mode}  note={batch.note!r}")
    print(
        f"{llm_cfg.model}: {elapsed:.0f} ms total"
        f"  (prefill {completion.prompt_ms:.0f} ms / {completion.prompt_tokens} tok,"
        f" generate {completion.eval_ms:.0f} ms / {completion.eval_tokens} tok"
        f" = {completion.tokens_per_second:.0f} tok/s)\n"
    )
    print(render(ops))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
