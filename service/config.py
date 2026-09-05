"""Configuration loading.

All tunables live in ``config.toml`` at the repository root so the system can be
retuned (different mic, different model, wider context window) without code edits.
"""
from __future__ import annotations

import json
import os
import pathlib
import tomllib
from dataclasses import dataclass, field
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 3000
    ssl_certfile: str = ""
    ssl_keyfile: str = ""


@dataclass(frozen=True)
class AudioConfig:
    device: str = ""
    sample_rate: int = 16000
    frame_ms: int = 32
    ffmpeg: str = "ffmpeg"

    @property
    def frame_samples(self) -> int:
        return int(self.sample_rate * self.frame_ms / 1000)

    @property
    def frame_bytes(self) -> int:
        return self.frame_samples * 2  # int16 mono


@dataclass(frozen=True)
class VadConfig:
    backend: str = "silero"
    threshold: float = 0.5
    silence_ms: int = 600
    min_speech_ms: int = 400
    max_utterance_ms: int = 25000
    preroll_ms: int = 300


@dataclass(frozen=True)
class AsrConfig:
    backend: str = "faster-whisper"
    model: str = "large-v3-turbo"
    device: str = "cpu"
    compute_type: str = "int8"
    cpu_threads: int = 10
    language: str = "ru"
    beam_size: int = 3
    prompt_context_chars: int = 220
    server_url: str = "http://127.0.0.1:8080"


@dataclass(frozen=True)
class LlmConfig:
    base_url: str = "http://127.0.0.1:11434"
    model: str = "qwen3:8b"
    temperature: float = 0.2
    num_ctx: int = 8192
    keep_alive: str = "30m"
    think: bool = False
    timeout_s: float = 120.0


@dataclass(frozen=True)
class ContextConfig:
    before: int = 6
    after: int = 2
    max_chars: int = 4000


@dataclass(frozen=True)
class TypographyConfig:
    dialogue_dash: bool = True
    quotes: bool = True
    ellipsis: bool = True
    spacing: bool = True
    nbsp_short_words: bool = False


@dataclass(frozen=True)
class HotkeyConfig:
    """Candidate key combinations, tried in order.

    A list rather than a single binding because Windows hands out global hotkeys
    first-come-first-served and gives no way to discover who holds one: Ctrl+Alt+Space
    was already taken on the development machine.
    """

    enabled: bool = True
    bindings: tuple[str, ...] = ("ctrl+alt+g", "ctrl+alt+q", "ctrl+alt+j", "pause")


@dataclass(frozen=True)
class DebugConfig:
    save_utterances: bool = False
    log_level: str = "INFO"


@dataclass(frozen=True)
class Project:
    """Book-specific vocabulary, used to bias ASR and inform the LLM."""

    title: str = "Untitled"
    language: str = "ru"
    notes: str = ""
    vocabulary: tuple[str, ...] = ()
    characters: tuple[str, ...] = ()
    places: tuple[str, ...] = ()

    @property
    def all_names(self) -> tuple[str, ...]:
        seen: dict[str, None] = {}
        for name in (*self.characters, *self.places, *self.vocabulary):
            if name.strip():
                seen[name.strip()] = None
        return tuple(seen)


@dataclass(frozen=True)
class Config:
    server: ServerConfig = field(default_factory=ServerConfig)
    audio: AudioConfig = field(default_factory=AudioConfig)
    vad: VadConfig = field(default_factory=VadConfig)
    asr: AsrConfig = field(default_factory=AsrConfig)
    llm: LlmConfig = field(default_factory=LlmConfig)
    context: ContextConfig = field(default_factory=ContextConfig)
    typography: TypographyConfig = field(default_factory=TypographyConfig)
    hotkey: HotkeyConfig = field(default_factory=HotkeyConfig)
    debug: DebugConfig = field(default_factory=DebugConfig)
    project: Project = field(default_factory=Project)
    root: pathlib.Path = ROOT


def _section(raw: dict[str, Any], name: str, cls: type) -> Any:
    data = raw.get(name) or {}
    fields = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
    kwargs = {k: v for k, v in data.items() if k in fields}
    for key, value in kwargs.items():
        if isinstance(value, list):
            kwargs[key] = tuple(value)
    return cls(**kwargs)


def _default_cert_pair() -> tuple[str, str]:
    """Locate the certificate pair installed by ``office-addin-dev-certs``."""
    home = pathlib.Path(os.path.expanduser("~")) / ".office-addin-dev-certs"
    crt, key = home / "localhost.crt", home / "localhost.key"
    if crt.exists() and key.exists():
        return str(crt), str(key)
    return "", ""


def load(root: pathlib.Path | None = None) -> Config:
    root = root or ROOT
    raw: dict[str, Any] = {}
    cfg_path = root / "config.toml"
    if cfg_path.exists():
        raw = tomllib.loads(cfg_path.read_text(encoding="utf-8"))

    server = _section(raw, "server", ServerConfig)
    if not server.ssl_certfile or not server.ssl_keyfile:
        crt, key = _default_cert_pair()
        server = ServerConfig(server.host, server.port,
                              server.ssl_certfile or crt, server.ssl_keyfile or key)

    project = Project()
    proj_path = root / "project.json"
    if proj_path.exists():
        try:
            pj = json.loads(proj_path.read_text(encoding="utf-8"))
            project = Project(
                title=pj.get("title", "Untitled"),
                language=pj.get("language", "ru"),
                notes=pj.get("notes", ""),
                vocabulary=tuple(pj.get("vocabulary") or ()),
                characters=tuple(pj.get("characters") or ()),
                places=tuple(pj.get("places") or ()),
            )
        except (json.JSONDecodeError, TypeError, AttributeError):
            pass  # a malformed project file must never stop the service from starting

    return Config(
        server=server,
        audio=_section(raw, "audio", AudioConfig),
        vad=_section(raw, "vad", VadConfig),
        asr=_section(raw, "asr", AsrConfig),
        llm=_section(raw, "llm", LlmConfig),
        context=_section(raw, "context", ContextConfig),
        typography=_section(raw, "typography", TypographyConfig),
        hotkey=_section(raw, "hotkey", HotkeyConfig),
        debug=_section(raw, "debug", DebugConfig),
        project=project,
        root=root,
    )
