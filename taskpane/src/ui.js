/**
 * Task pane rendering.
 *
 * The pane's job is to make the system's state legible at a glance while the user is
 * looking at their document, not at it: is it listening, what can it see, what did it
 * just hear, and how long did that take.
 */

const $ = (id) => document.getElementById(id);

export class Ui {
  constructor() {
    this.el = {
      boot: $("boot"),
      app: $("app"),
      link: $("link"),
      toggle: $("toggle"),
      toggleLabel: $("toggle-label"),
      hotkey: $("hotkey"),
      asr: $("s-asr"),
      llm: $("s-llm"),
      mic: $("s-mic"),
      timingRow: $("row-timing"),
      timing: $("s-timing"),
      context: $("context"),
      before: $("ctx-before"),
      after: $("ctx-after"),
      log: $("log"),
      undo: $("undo"),
      dictate: $("dictate"),
      send: $("send"),
      fatal: $("fatal"),
    };
    this.listening = false;
  }

  ready() {
    this.el.boot.hidden = true;
    this.el.app.hidden = false;
  }

  fatal(message) {
    this.el.boot.hidden = true;
    this.el.app.hidden = false;
    this.el.fatal.hidden = false;
    this.el.fatal.textContent = message;
  }

  setLink(connected) {
    this.el.link.textContent = connected ? "служба подключена" : "нет связи со службой";
    this.el.link.className = `pill ${connected ? "pill--on" : "pill--off"}`;
    this.el.toggle.disabled = !connected;
  }

  setState(state) {
    this.listening = Boolean(state.listening);
    this.el.toggle.classList.toggle("is-live", this.listening);
    this.el.toggle.setAttribute("aria-pressed", String(this.listening));
    this.el.toggleLabel.textContent = this.listening
      ? state.busy
        ? "Слушаю · обрабатываю…"
        : "Слушаю — нажмите, чтобы остановить"
      : "Начать диктовку";

    this.el.hotkey.textContent = state.hotkey || "не назначена";

    const asrOk = Boolean(state.asrReady);
    this.el.asr.textContent = state.asrError
      ? state.asrError
      : `${state.asrModel || "—"}${asrOk ? "" : " · загрузка…"}`;
    this.el.asr.classList.toggle("is-bad", Boolean(state.asrError));

    const llmBad = String(state.llmStatus || "").startsWith("unavailable");
    this.el.llm.textContent = state.llmStatus || state.llmModel || "—";
    this.el.llm.classList.toggle("is-bad", llmBad);

    this.el.mic.textContent = state.device || "—";

    if (Number.isFinite(state.contextBefore) && document.activeElement !== this.el.before) {
      this.el.before.value = state.contextBefore;
    }
    if (Number.isFinite(state.contextAfter) && document.activeElement !== this.el.after) {
      this.el.after.value = state.contextAfter;
    }
  }

  setTiming(timing) {
    const parts = [];
    if (timing.asrMs) parts.push(`распознавание ${Math.round(timing.asrMs)} мс`);
    if (timing.llmMs) parts.push(`модель ${Math.round(timing.llmMs)} мс`);
    if (timing.rtf) parts.push(`×${timing.rtf}`);
    if (!parts.length) return;
    this.el.timingRow.hidden = false;
    this.el.timing.textContent = parts.join(" · ");
  }

  setUndoEnabled(enabled) {
    this.el.undo.disabled = !enabled;
  }

  renderContext(context) {
    const list = document.createDocumentFragment();
    for (const paragraph of context.paragraphs || []) {
      const line = document.createElement("div");
      line.className = "ctx-line" + (paragraph.id === "P0" ? " ctx-line--anchor" : "");

      const id = document.createElement("span");
      id.className = "ctx-id";
      id.textContent = paragraph.id;
      line.append(id);

      const body = document.createElement("span");
      body.className = "ctx-text";
      if (paragraph.style && paragraph.style !== "normal") {
        const tag = document.createElement("span");
        tag.className = "ctx-style";
        tag.textContent = `${paragraph.style} `;
        body.append(tag);
      }
      if (!paragraph.text) {
        const empty = document.createElement("i");
        empty.className = "empty";
        empty.textContent = "пустой абзац";
        body.append(empty);
        if (paragraph.caret !== undefined) body.append(caretNode());
      } else if (paragraph.caret === undefined) {
        body.append(paragraph.text);
      } else {
        const at = Math.max(0, Math.min(paragraph.text.length, paragraph.caret));
        body.append(paragraph.text.slice(0, at), caretNode(), paragraph.text.slice(at));
      }
      line.append(body);
      list.append(line);
    }
    this.el.context.replaceChildren(list);
  }

  log(text, kind = "") {
    const item = document.createElement("li");
    if (kind) item.className = kind;
    const time = document.createElement("span");
    time.className = "t";
    time.textContent = new Date().toLocaleTimeString("ru-RU", { hour12: false });
    item.append(time, document.createTextNode(text));
    this.el.log.prepend(item);
    while (this.el.log.childElementCount > 80) this.el.log.lastElementChild.remove();
  }
}

function caretNode() {
  const caret = document.createElement("span");
  caret.className = "ctx-caret";
  return caret;
}
