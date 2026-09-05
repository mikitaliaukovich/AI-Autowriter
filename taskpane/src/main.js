/**
 * Task pane entry point: wires Word, the local service and the UI together.
 *
 * Deliberately absent: any audio handling. The microphone is captured by the local
 * service, not here — Word task panes run inside WebView2, where `getUserMedia`
 * permission prompts are unreliable and there is no way for the user to grant access
 * after the fact.
 */

import { applyOps, describeError, undo } from "./apply.js";
import { readWindow } from "./context.js";
import { Journal } from "./journal.js";
import { Ui } from "./ui.js";
import { Link } from "./ws.js";

const SELECTION_DEBOUNCE_MS = 250;

const ui = new Ui();
const journal = new Journal();
const link = new Link("/ws");

const settings = { before: 6, after: 2 };
const caps = { withStyle: true, wordApi13: true };

let selectionTimer = null;
let contextInFlight = false;

/* --- Word capabilities ---------------------------------------------------------- */

function detectCapabilities() {
  const supports = (set, version) => {
    try {
      return Office.context.requirements.isSetSupported(set, version);
    } catch {
      return false;
    }
  };
  caps.wordApi13 = supports("WordApi", "1.3");
  caps.withStyle = caps.wordApi13;
  return {
    "1.1": supports("WordApi", "1.1"),
    "1.3": caps.wordApi13,
    "1.4": supports("WordApi", "1.4"),
    "1.5": supports("WordApi", "1.5"),
  };
}

/* --- context ------------------------------------------------------------------- */

async function sendContext(reqId) {
  if (contextInFlight && !reqId) return;
  contextInFlight = true;
  try {
    const context = await readWindow({ ...settings, withStyle: caps.withStyle });
    ui.renderContext(context);
    link.send({ type: "context", ...(reqId ? { reqId } : {}), ...context });
  } catch (error) {
    const detail = describeError(error);
    ui.log(`Не удалось прочитать документ: ${detail}`, "error");
    if (reqId) link.send({ type: "context", reqId, paragraphs: [], error: detail });
  } finally {
    contextInFlight = false;
  }
}

function scheduleContext() {
  if (selectionTimer) clearTimeout(selectionTimer);
  selectionTimer = setTimeout(() => {
    selectionTimer = null;
    sendContext(null);
  }, SELECTION_DEBOUNCE_MS);
}

/* --- service messages ----------------------------------------------------------- */

const handlers = {
  requestContext: (message) => sendContext(message.reqId),

  apply: async (message) => {
    const result = await applyOps(message.ops || [], journal, { withStyle: caps.withStyle });
    link.send({ type: "applyResult", reqId: message.reqId, result });
    ui.setUndoEnabled(journal.canUndo());

    if (result.ok && result.applied) {
      ui.log(describeApplied(message, result), "done");
    } else if (result.conflicts?.length) {
      ui.log(`Правка отклонена: ${result.conflicts[0].reason}`, "warn");
    } else if (result.error) {
      ui.log(`Ошибка правки: ${result.error}`, "error");
    }
    await sendContext(null);
  },

  state: (message) => ui.setState(message),

  transcript: (message) => {
    if (!message.text) return;
    ui.log(message.text, message.kind === "dropped" ? "dropped" : "said");
  },

  timing: (message) => ui.setTiming(message),

  log: (message) => ui.log(message.message, message.level === "info" ? "" : message.level),
};

function describeApplied(message, result) {
  const note = message.meta?.note;
  const count = result.applied === 1 ? "1 правка" : `${result.applied} правки`;
  return note ? `${count} · ${note}` : count;
}

/* --- user actions ---------------------------------------------------------------- */

function wireControls() {
  ui.el.toggle.addEventListener("click", () => {
    link.send({ type: "command", name: "toggle" });
  });

  ui.el.undo.addEventListener("click", async () => {
    const result = await undo(journal, 1, { withStyle: caps.withStyle });
    ui.setUndoEnabled(journal.canUndo());
    ui.log(result.ok ? "Правка отменена" : `Не удалось отменить: ${result.error}`, result.ok ? "done" : "warn");
    await sendContext(null);
  });

  ui.el.send.addEventListener("click", () => {
    const text = ui.el.dictate.value.trim();
    if (!text) return;
    link.send({ type: "dictate", text });
    ui.el.dictate.value = "";
  });

  ui.el.dictate.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) ui.el.send.click();
  });

  for (const [key, input] of [["before", ui.el.before], ["after", ui.el.after]]) {
    input.addEventListener("change", () => {
      const value = Number.parseInt(input.value, 10);
      if (Number.isNaN(value)) return;
      settings[key] = Math.max(0, Math.min(20, value));
      input.value = settings[key];
      sendContext(null);
    });
  }
}

/* --- boot ------------------------------------------------------------------------ */

Office.onReady(async (info) => {
  if (info.host !== Office.HostType.Word) {
    ui.fatal("Эта надстройка работает только в Microsoft Word.");
    return;
  }

  ui.ready();
  wireControls();
  ui.setUndoEnabled(false);
  journal.onChange = (j) => ui.setUndoEnabled(j.canUndo());

  const wordApi = detectCapabilities();
  if (!wordApi["1.1"]) {
    ui.fatal("Эта версия Word не поддерживает нужный набор Word API (1.1).");
    return;
  }
  if (!caps.wordApi13) {
    ui.log("Word API 1.3 недоступен: стили абзацев и точная позиция курсора отключены.", "warn");
  }

  link.onStatus = (connected) => {
    ui.setLink(connected);
    if (!connected) return;
    link.send({
      type: "hello",
      wordApi,
      platform: String(Office.context.platform ?? ""),
      version: String(Office.context.diagnostics?.version ?? ""),
      docTitle: "",
    });
    sendContext(null);
  };

  link.onMessage = async (message) => {
    const handler = handlers[message.type];
    if (!handler) return;
    try {
      await handler(message);
    } catch (error) {
      ui.log(`Сбой обработки «${message.type}»: ${describeError(error)}`, "error");
    }
  };

  link.connect();

  // The Common API selection event is available on every Word build; the newer
  // Document.onSelectionChanged needs a requirement set this Office may not have.
  Office.context.document.addHandlerAsync(
    Office.EventType.DocumentSelectionChanged,
    scheduleContext,
    (result) => {
      if (result.status === Office.AsyncResultStatus.Failed) {
        ui.log("Не удалось отследить перемещение курсора — окно контекста обновляется только после правок.", "warn");
      }
    },
  );
});
