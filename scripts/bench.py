"""Score models and prompt changes against the Russian fixture set.

Two jobs in one:

* **Regression check** — after editing the system prompt or the few-shot examples, run
  this to see whether anything that used to work stopped working.
* **Model choice** — run it across several models to decide with numbers rather than
  impressions which one to configure.

    python scripts/bench.py
    python scripts/bench.py --models qwen3:4b,qwen3:8b,qwen3:14b --repeat 3
    python scripts/bench.py --only dialogue-pair --verbose

Each fixture asserts what actually matters — where the edit landed, what survived the
cleanup, what must not appear — rather than an exact string, because there are many
acceptable ways to write the same sentence.
"""
from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import pathlib
import statistics
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from service import config as config_module  # noqa: E402
from service.console import enable_utf8  # noqa: E402
from service.llm import prompts  # noqa: E402
from service.llm.ollama_client import LlmError, OllamaClient  # noqa: E402
from service.llm.schema import LLM_RESPONSE_SCHEMA, parse_ops  # noqa: E402
from service.pipeline import commands  # noqa: E402
from service.pipeline.finalize import finalize_ops  # noqa: E402
from service.pipeline.typography import EM_DASH  # noqa: E402
from service.textspec import build_context  # noqa: E402

FIXTURES = pathlib.Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "ru_cases.jsonl"


@dataclasses.dataclass
class Outcome:
    fixture: str
    ok: bool
    latency_ms: float
    failures: list[str]
    ops: list[dict]


def op_signature(ops: list[dict]) -> list[str]:
    return [f"{op['op']}:{op['id']}" if op.get("id") else op["op"] for op in ops]


def all_text(ops: list[dict]) -> str:
    parts: list[str] = []
    for op in ops:
        if "text" in op:
            parts.append(op["text"])
        if "replace" in op:
            parts.append(op["replace"])
        for paragraph in op.get("paragraphs", []):
            parts.append(paragraph["text"])
    return "\n".join(parts)


def check(fixture: dict, mode: str, ops: list[dict]) -> list[str]:
    """Return a list of failure descriptions, empty when the case passes."""
    failures: list[str] = []

    if fixture.get("mode") and mode != fixture["mode"]:
        failures.append(f"mode {mode!r}, expected {fixture['mode']!r}")

    if fixture.get("no_document_change"):
        if ops:
            failures.append(f"expected no change, got {op_signature(ops)}")
        return failures

    expected_ops = fixture.get("ops") or []
    if expected_ops:
        signature = op_signature(ops)
        # Each entry may offer alternatives separated by "|": several op shapes are
        # equally correct ways to land the same edit.
        satisfied = any(
            any(alternative and alternative in signature for alternative in entry.split("|"))
            for entry in expected_ops
        )
        if not satisfied:
            failures.append(f"ops {signature}, expected one of {expected_ops}")

    text = all_text(ops)

    for needle in fixture.get("contains", []):
        if not any(alternative in text for alternative in needle.split("|")):
            failures.append(f"missing {needle!r}")

    for needle in fixture.get("not_contains", []):
        if needle in text:
            failures.append(f"should not contain {needle!r}")

    if fixture.get("starts_with_dash"):
        lines = [t for t in text.splitlines() if t.strip()]
        if not lines or not all(line.lstrip().startswith(EM_DASH) for line in lines):
            failures.append("dialogue lines must start with an em dash")

    if (want := fixture.get("paragraphs")) is not None:
        produced = sum(len(op.get("paragraphs", [])) or (1 if "text" in op else 0) for op in ops)
        if produced < want:
            failures.append(f"{produced} paragraphs, expected at least {want}")

    if fixture.get("heading"):
        styled = any(
            op.get("style", "").startswith("heading")
            or any(p.get("style", "").startswith("heading") for p in op.get("paragraphs", []))
            for op in ops
        )
        if not styled:
            failures.append("expected a heading style")

    if (limit := fixture.get("max_chars")) is not None and len(text) > limit:
        failures.append(f"{len(text)} characters, expected at most {limit} (invented content?)")

    return failures


async def run_case(client: OllamaClient, cfg, fixture: dict) -> Outcome:
    context = build_context(fixture.get("context", ""))
    parsed = commands.parse(fixture["utterance"])

    if parsed.kind == "control":
        return Outcome(fixture["id"], True, 0.0, [], [])

    messages = prompts.build_messages(
        context, parsed.text, kind=parsed.kind, project=cfg.project, max_chars=cfg.context.max_chars
    )
    started = time.perf_counter()
    try:
        completion = await client.complete_json(messages, LLM_RESPONSE_SCHEMA)
    except LlmError as exc:
        return Outcome(fixture["id"], False, 0.0, [f"LLM error: {exc}"], [])
    latency_ms = (time.perf_counter() - started) * 1000

    batch = parse_ops(completion.payload, context.ids)
    ops = finalize_ops(batch, context, cfg.typography)
    failures = check(fixture, batch.mode, ops)
    return Outcome(fixture["id"], not failures, latency_ms, failures, ops)


async def run_model(model: str, fixtures: list[dict], cfg, repeat: int, verbose: bool) -> dict:
    client = OllamaClient(dataclasses.replace(cfg.llm, model=model))
    outcomes: list[Outcome] = []
    try:
        ready, detail = await client.health()
        if not ready:
            print(f"  {model}: unavailable — {detail}")
            return {"model": model, "available": False}

        await client.warmup()
        for run in range(repeat):
            for fixture in fixtures:
                outcome = await run_case(client, cfg, fixture)
                outcomes.append(outcome)
                mark = "ok  " if outcome.ok else "FAIL"
                suffix = f" ({run + 1})" if repeat > 1 else ""
                print(f"  {mark} {outcome.fixture}{suffix}  {outcome.latency_ms:.0f} ms")
                for failure in outcome.failures:
                    print(f"        - {failure}")
                if verbose and outcome.ops:
                    for line in json.dumps(outcome.ops, ensure_ascii=False, indent=2).splitlines():
                        print(f"        {line}")
    finally:
        await client.aclose()

    latencies = sorted(o.latency_ms for o in outcomes if o.latency_ms)
    passed = sum(1 for o in outcomes if o.ok)
    return {
        "model": model,
        "available": True,
        "passed": passed,
        "total": len(outcomes),
        "p50": statistics.median(latencies) if latencies else 0.0,
        "p95": latencies[int(len(latencies) * 0.95) - 1] if len(latencies) > 1 else (latencies[0] if latencies else 0.0),
        "failed_ids": sorted({o.fixture for o in outcomes if not o.ok}),
    }


async def main() -> int:
    enable_utf8()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--models", default="", help="comma-separated; defaults to the configured model")
    parser.add_argument("--repeat", type=int, default=1, help="runs per fixture, to see variance")
    parser.add_argument("--only", default="", help="run a single fixture by id")
    parser.add_argument("--verbose", action="store_true", help="print the ops for each case")
    args = parser.parse_args()

    cfg = config_module.load()
    fixtures = [json.loads(line) for line in FIXTURES.read_text(encoding="utf-8").splitlines() if line.strip()]
    if args.only:
        fixtures = [f for f in fixtures if f["id"] == args.only]
        if not fixtures:
            print(f"No fixture with id {args.only!r}", file=sys.stderr)
            return 2

    models = [m.strip() for m in args.models.split(",") if m.strip()] or [cfg.llm.model]
    summaries = []
    for model in models:
        print(f"\n{model}")
        summaries.append(await run_model(model, fixtures, cfg, args.repeat, args.verbose))

    print("\n" + "=" * 68)
    print(f"{'model':<16} {'passed':>10} {'p50 ms':>9} {'p95 ms':>9}   failures")
    print("-" * 68)
    for summary in summaries:
        if not summary.get("available"):
            print(f"{summary['model']:<16} {'unavailable':>10}")
            continue
        rate = f"{summary['passed']}/{summary['total']}"
        failures = ", ".join(summary["failed_ids"]) or "—"
        print(f"{summary['model']:<16} {rate:>10} {summary['p50']:>9.0f} {summary['p95']:>9.0f}   {failures}")

    worst = [s for s in summaries if s.get("available") and s["passed"] < s["total"]]
    return 1 if worst and len(summaries) == 1 else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
