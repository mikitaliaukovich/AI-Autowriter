"""Global hotkey via the Win32 API.

Implemented with ``ctypes`` and ``RegisterHotKey`` rather than a third-party keyboard
library: it needs no dependency, no administrator rights, and no low-level keyboard
hook (which antivirus software tends to dislike). It works while Word has focus, which
is the entire point — the user dictates into Word, not into the task pane.

``RegisterHotKey`` binds to the thread that called it and delivers ``WM_HOTKEY`` to
that thread's message queue, so the listener owns a dedicated thread and marshals
presses back onto the asyncio loop.

Combinations are frequently already owned by other software (Ctrl+Alt+Space was taken
on the development machine), and Windows gives no way to find out by whom. So the
config lists several candidates and the first one Windows grants wins; the task pane is
told which it ended up with.
"""
from __future__ import annotations

import ctypes
import logging
import threading
from ctypes import wintypes
from typing import Callable

log = logging.getLogger(__name__)

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
_user32.RegisterHotKey.restype = wintypes.BOOL
_user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
_user32.UnregisterHotKey.restype = wintypes.BOOL

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000

WM_HOTKEY = 0x0312
WM_QUIT = 0x0012

ERROR_HOTKEY_ALREADY_REGISTERED = 1409

_MODIFIERS = {
    "alt": MOD_ALT,
    "ctrl": MOD_CONTROL,
    "control": MOD_CONTROL,
    "shift": MOD_SHIFT,
    "win": MOD_WIN,
    "windows": MOD_WIN,
    "cmd": MOD_WIN,
}

_NAMED_KEYS = {
    "space": 0x20,
    "enter": 0x0D,
    "return": 0x0D,
    "tab": 0x09,
    "escape": 0x1B,
    "esc": 0x1B,
    "backspace": 0x08,
    "insert": 0x2D,
    "delete": 0x2E,
    "home": 0x24,
    "end": 0x23,
    "pageup": 0x21,
    "pagedown": 0x22,
    "left": 0x25,
    "up": 0x26,
    "right": 0x27,
    "down": 0x28,
    "pause": 0x13,
    "scrolllock": 0x91,
    "numlock": 0x90,
    **{f"f{n}": 0x6F + n for n in range(1, 25)},
}

_PRETTY = {"ctrl": "Ctrl", "control": "Ctrl", "alt": "Alt", "shift": "Shift", "win": "Win", "cmd": "Win"}


class HotkeyError(RuntimeError):
    pass


class Binding:
    """One parsed key combination, e.g. ``ctrl+alt+g``."""

    __slots__ = ("flags", "vk", "label")

    def __init__(self, spec: str) -> None:
        parts = [p.strip().lower() for p in spec.split("+") if p.strip()]
        if not parts:
            raise HotkeyError(f"empty hotkey specification: {spec!r}")

        *modifier_names, key_name = parts
        flags = MOD_NOREPEAT
        pretty: list[str] = []
        for name in modifier_names:
            if name not in _MODIFIERS:
                raise HotkeyError(f"unknown modifier {name!r} in {spec!r}")
            flags |= _MODIFIERS[name]
            pretty.append(_PRETTY.get(name, name.capitalize()))

        if key_name in _NAMED_KEYS:
            vk = _NAMED_KEYS[key_name]
        elif len(key_name) == 1:
            vk = ord(key_name.upper())
        else:
            raise HotkeyError(f"unknown key {key_name!r} in {spec!r}")

        self.flags = flags
        self.vk = vk
        self.label = "+".join([*pretty, key_name.capitalize()])

    def __repr__(self) -> str:
        return f"Binding({self.label!r})"


def parse_bindings(specs: tuple[str, ...] | list[str]) -> list[Binding]:
    bindings: list[Binding] = []
    for spec in specs:
        try:
            bindings.append(Binding(spec))
        except HotkeyError as exc:
            log.warning("ignoring hotkey: %s", exc)
    return bindings


class HotkeyListener:
    """Runs a Win32 message loop on its own thread and calls ``on_press``.

    Registers the first binding Windows grants. ``label`` is empty until :meth:`start`
    has succeeded.
    """

    def __init__(self, specs: tuple[str, ...] | list[str], on_press: Callable[[], None]) -> None:
        self._bindings = parse_bindings(specs)
        self._on_press = on_press
        self._thread: threading.Thread | None = None
        self._thread_id: int | None = None
        self._ready = threading.Event()
        self._error: str | None = None
        self.label: str = ""

    def start(self) -> str:
        """Register and begin listening. Returns the label of the binding that won."""
        if not self._bindings:
            raise HotkeyError("no valid hotkey combinations configured")
        if self._thread is not None:
            return self.label

        self._thread = threading.Thread(target=self._run, name="hotkey", daemon=True)
        self._thread.start()
        self._ready.wait(timeout=5.0)
        if self._error:
            self._thread = None
            raise HotkeyError(self._error)
        return self.label

    def stop(self) -> None:
        thread, self._thread = self._thread, None
        if thread is None or self._thread_id is None:
            return
        _user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
        thread.join(timeout=2.0)
        self._thread_id = None
        self.label = ""

    def _register_first_available(self, hotkey_id: int) -> Binding | None:
        taken: list[str] = []
        for binding in self._bindings:
            if _user32.RegisterHotKey(None, hotkey_id, binding.flags, binding.vk):
                if taken:
                    log.info("hotkey %s already in use; using %s", ", ".join(taken), binding.label)
                return binding
            code = ctypes.get_last_error()
            if code == ERROR_HOTKEY_ALREADY_REGISTERED:
                taken.append(binding.label)
            else:
                taken.append(f"{binding.label} (Win32 error {code})")
        self._error = (
            "every configured hotkey is already owned by another application: "
            + ", ".join(taken)
            + ". Add a free combination to [hotkey].bindings in config.toml."
        )
        return None

    def _run(self) -> None:
        self._thread_id = ctypes.windll.kernel32.GetCurrentThreadId()
        hotkey_id = 1

        binding = self._register_first_available(hotkey_id)
        if binding is None:
            self._ready.set()
            return

        self.label = binding.label
        self._ready.set()
        log.info("hotkey registered: %s", self.label)

        message = wintypes.MSG()
        try:
            while _user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
                if message.message == WM_HOTKEY:
                    try:
                        self._on_press()
                    except Exception:
                        log.exception("hotkey handler failed")
        finally:
            _user32.UnregisterHotKey(None, hotkey_id)
            log.debug("hotkey unregistered: %s", binding.label)
