"""Thin async client for Ollama's chat API.

Ollama is already running with ROCm on this machine, so the LLM is the only component
that touches the GPU. Two settings matter more than anything else here:

* ``think: false`` — qwen3 is a hybrid-reasoning model. Left on, it emits a long
  reasoning block before the answer and roughly triples time-to-first-token, which is
  the difference between a usable stenographer and an unusable one.
* ``format`` — a JSON schema, which Ollama turns into a decoding grammar. The model
  physically cannot emit malformed JSON, so no repair pass is needed.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx

from service.config import LlmConfig

log = logging.getLogger(__name__)


class LlmError(RuntimeError):
    pass


@dataclass
class Completion:
    payload: dict[str, Any]
    raw: str
    latency_ms: float
    eval_tokens: int = 0
    prompt_tokens: int = 0
    prompt_ms: float = 0.0
    eval_ms: float = 0.0

    @property
    def tokens_per_second(self) -> float:
        return self.eval_tokens / (self.eval_ms / 1000) if self.eval_ms else 0.0


class OllamaClient:
    def __init__(self, cfg: LlmConfig) -> None:
        self.cfg = cfg
        self._client = httpx.AsyncClient(
            base_url=cfg.base_url.rstrip("/"),
            timeout=httpx.Timeout(cfg.timeout_s, connect=5.0),
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def available_models(self) -> list[str]:
        try:
            response = await self._client.get("/api/tags", timeout=5.0)
            response.raise_for_status()
        except Exception as exc:
            raise LlmError(f"Ollama unreachable at {self.cfg.base_url}: {exc}") from exc
        return [m.get("name", "") for m in response.json().get("models", [])]

    async def health(self) -> tuple[bool, str]:
        """Return (ready, detail). Never raises — the pane shows the detail verbatim."""
        try:
            models = await self.available_models()
        except LlmError as exc:
            return False, str(exc)
        if self.cfg.model not in models:
            # Ollama accepts "qwen3:8b" for a model listed as "qwen3:8b"; be lenient
            # about a missing ":latest" suffix either way.
            stem = self.cfg.model.split(":")[0]
            if not any(m.split(":")[0] == stem for m in models):
                return False, f"model '{self.cfg.model}' not pulled (ollama pull {self.cfg.model})"
        return True, self.cfg.model

    async def warmup(self) -> None:
        """Load the model into VRAM so the first real utterance is not the slow one."""
        try:
            await self._post(
                {
                    "model": self.cfg.model,
                    "messages": [{"role": "user", "content": "ок"}],
                    "stream": False,
                    "think": self.cfg.think,
                    "keep_alive": self.cfg.keep_alive,
                    "options": {"num_predict": 1, "num_ctx": self.cfg.num_ctx},
                },
                timeout=180.0,
            )
            log.info("LLM warm: %s", self.cfg.model)
        except Exception as exc:
            log.warning("LLM warmup failed: %s", exc)

    async def _post(self, body: dict[str, Any], timeout: float | None = None) -> dict[str, Any]:
        try:
            response = await self._client.post(
                "/api/chat", json=body, timeout=timeout or self.cfg.timeout_s
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise LlmError(f"Ollama returned {exc.response.status_code}: {exc.response.text[:300]}") from exc
        except Exception as exc:
            raise LlmError(f"Ollama request failed: {exc}") from exc
        return response.json()

    async def complete_json(
        self,
        messages: list[dict[str, str]],
        schema: dict[str, Any],
        *,
        max_tokens: int = 900,
    ) -> Completion:
        """One constrained-JSON completion."""
        started = time.perf_counter()
        data = await self._post(
            {
                "model": self.cfg.model,
                "messages": messages,
                "stream": False,
                "format": schema,
                "think": self.cfg.think,
                "keep_alive": self.cfg.keep_alive,
                "options": {
                    "temperature": self.cfg.temperature,
                    "num_ctx": self.cfg.num_ctx,
                    "num_predict": max_tokens,
                },
            }
        )
        latency_ms = (time.perf_counter() - started) * 1000
        raw = (data.get("message") or {}).get("content") or ""
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            # Should be impossible under a grammar, but a truncated response (hitting
            # num_predict mid-object) can still land here.
            raise LlmError(f"model returned non-JSON ({exc}): {raw[:300]}") from exc
        if not isinstance(payload, dict):
            raise LlmError(f"model returned {type(payload).__name__}, expected object")

        # Ollama reports durations in nanoseconds. Splitting prefill from generation
        # is what tells you whether to shorten the prompt or shrink the model.
        return Completion(
            payload=payload,
            raw=raw,
            latency_ms=latency_ms,
            eval_tokens=int(data.get("eval_count") or 0),
            prompt_tokens=int(data.get("prompt_eval_count") or 0),
            prompt_ms=float(data.get("prompt_eval_duration") or 0) / 1e6,
            eval_ms=float(data.get("eval_duration") or 0) / 1e6,
        )
