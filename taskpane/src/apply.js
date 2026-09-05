/**
 * Applying edit operations to the document.
 *
 * Two invariants make this safe to run against someone's manuscript:
 *
 * 1. **Verify before mutating.** Every op carries the hash of the paragraph text as it
 *    was when the context window was read. All hashes are checked up front, so a batch
 *    either applies wholly to the document it was computed against, or not at all. If
 *    the user typed something in the meantime the batch is rejected and the service
 *    recomputes it against the document as it actually is.
 * 2. **Record the reversal.** Nothing is changed without pushing the means to undo it
 *    onto the journal first.
 *
 * After a batch the caret is moved to the end of the last thing written, which is what
 * makes the context window follow the dictation.
 */

import { cleanText, hashText, resolveIds, readWindow } from "./context.js";
import { removeAction, reinsertAction, restoreAction } from "./journal.js";
import { fromWord, toWord } from "./styles.js";

/** Word's search() rejects longer patterns, and treats "^" as an escape introducer. */
const MAX_SEARCH = 255;

function escapeForSearch(text) {
  return text.replace(/\^/g, "^^");
}

class Conflict extends Error {
  constructor(index, id, reason) {
    super(reason);
    this.index = index;
    this.id = id;
    this.reason = reason;
  }
}

/**
 * Check every op's `expect` hash against the document as it is right now.
 * @returns {Promise<object[]>} conflicts, empty when the batch is safe to apply
 */
async function verify(ops, paragraphs) {
  const conflicts = [];
  for (let i = 0; i < ops.length; i += 1) {
    const op = ops[i];
    if (!op.id) continue;

    const paragraph = paragraphs.get(op.id);
    if (!paragraph) {
      conflicts.push({ index: i, id: op.id, reason: "абзац вне окна контекста" });
      continue;
    }
    if (!op.expect) continue;

    const actual = await hashText(cleanText(paragraph.text));
    if (actual !== op.expect) {
      conflicts.push({ index: i, id: op.id, reason: "абзац изменился после чтения контекста" });
    }
  }
  return conflicts;
}

async function runOp(context, op, paragraphs, actions, withStyle) {
  const paragraph = op.id ? paragraphs.get(op.id) : null;
  const beforeText = paragraph ? cleanText(paragraph.text) : "";
  const beforeStyle = paragraph && withStyle ? fromWord(paragraph.styleBuiltIn) : "normal";

  switch (op.op) {
    case "append_to_paragraph": {
      paragraph.insertText(op.text, Word.InsertLocation.end);
      await context.sync();
      actions.push(restoreAction(beforeText + op.text, beforeText, beforeStyle));
      return paragraph;
    }

    case "insert_paragraphs_after": {
      let reference = paragraph;
      for (const item of op.paragraphs) {
        reference = reference.insertParagraph(item.text, Word.InsertLocation.after);
        const style = withStyle ? toWord(item.style) : null;
        if (style) reference.styleBuiltIn = style;
      }
      await context.sync();
      for (const item of op.paragraphs) actions.push(removeAction(item.text));
      return reference;
    }

    case "replace_paragraph": {
      paragraph.insertText(op.text, Word.InsertLocation.replace);
      const style = withStyle && op.style ? toWord(op.style) : null;
      if (style) paragraph.styleBuiltIn = style;
      await context.sync();
      actions.push(restoreAction(op.text, beforeText, beforeStyle));
      return paragraph;
    }

    case "replace_in_paragraph": {
      if (op.find.length > MAX_SEARCH) throw new Conflict(-1, op.id, "фрагмент длиннее 255 символов");
      const found = paragraph.search(escapeForSearch(op.find), { matchCase: true });
      found.load("items");
      await context.sync();
      if (!found.items.length) throw new Conflict(-1, op.id, `фрагмент не найден: «${op.find.slice(0, 40)}»`);
      found.items[0].insertText(op.replace, Word.InsertLocation.replace);
      await context.sync();
      paragraph.load("text");
      await context.sync();
      actions.push(restoreAction(cleanText(paragraph.text), beforeText, beforeStyle));
      return paragraph;
    }

    case "delete_paragraph": {
      const previous = paragraph.getPreviousOrNullObject();
      previous.load("text,isNullObject");
      await context.sync();
      const anchorText = previous.isNullObject ? null : cleanText(previous.text);
      paragraph.delete();
      await context.sync();
      actions.push(reinsertAction(beforeText, beforeStyle, anchorText));
      return previous.isNullObject ? null : previous;
    }

    case "set_style": {
      const style = toWord(op.style);
      if (!withStyle || !style) throw new Conflict(-1, op.id, "стили недоступны в этой версии Word");
      paragraph.styleBuiltIn = style;
      await context.sync();
      actions.push(restoreAction(beforeText, beforeText, beforeStyle));
      return paragraph;
    }

    default:
      throw new Conflict(-1, op.id || "", `неизвестная операция «${op.op}»`);
  }
}

/** Move the caret to the end of the last paragraph we touched. */
async function anchorAt(context, paragraph) {
  if (!paragraph) return;
  try {
    paragraph.getRange("End").select();
    await context.sync();
  } catch {
    // getRange needs WordApi 1.3; selecting the paragraph is a usable fallback.
    try {
      paragraph.select();
      await context.sync();
    } catch {
      /* leave the caret where it is */
    }
  }
}

/**
 * Apply a batch of ops.
 *
 * @param {object[]} ops
 * @param {import("./journal.js").Journal} journal
 * @param {{withStyle: boolean}} caps
 * @returns {Promise<{ok: boolean, applied: number, conflicts: object[], error: string}>}
 */
export async function applyOps(ops, journal, { withStyle = true } = {}) {
  const real = ops.filter((op) => op.op !== "noop");
  if (!real.length) return { ok: true, applied: 0, conflicts: [], error: "" };

  const revert = real.find((op) => op.op === "revert");
  if (revert) {
    const count = Math.max(1, Number(revert.count) || 1);
    return undo(journal, count, { withStyle });
  }

  try {
    return await Word.run(async (context) => {
      const ids = real.map((op) => op.id).filter(Boolean);
      const paragraphs = await resolveIds(context, ids, withStyle);

      const conflicts = await verify(real, paragraphs);
      if (conflicts.length) {
        return { ok: false, applied: 0, conflicts, error: "" };
      }

      const actions = [];
      let last = null;
      let applied = 0;
      try {
        for (const op of real) {
          last = (await runOp(context, op, paragraphs, actions, withStyle)) ?? last;
          applied += 1;
        }
      } catch (error) {
        // Whatever did land is still recorded, so the user can undo a partial batch.
        if (actions.length) journal.record(actions, "частичная правка");
        if (error instanceof Conflict) {
          return { ok: false, applied, conflicts: [{ index: applied, id: error.id, reason: error.reason }], error: "" };
        }
        throw error;
      }

      journal.record(actions, describeBatch(real));
      await anchorAt(context, last);
      return { ok: true, applied, conflicts: [], error: "" };
    });
  } catch (error) {
    return { ok: false, applied: 0, conflicts: [], error: describeError(error) };
  }
}

function describeBatch(ops) {
  const first = ops[0];
  switch (first?.op) {
    case "append_to_paragraph":
      return "дописан текст";
    case "insert_paragraphs_after":
      return `добавлено абзацев: ${first.paragraphs.length}`;
    case "replace_paragraph":
    case "replace_in_paragraph":
      return "заменён текст";
    case "delete_paragraph":
      return "удалён абзац";
    case "set_style":
      return "изменён стиль";
    default:
      return "правка";
  }
}

export function describeError(error) {
  if (!error) return "неизвестная ошибка";
  const parts = [error.message || String(error)];
  if (error.debugInfo?.message && error.debugInfo.message !== error.message) {
    parts.push(error.debugInfo.message);
  }
  return parts.join(" — ");
}

/**
 * Reverse the last `count` journal entries.
 *
 * Paragraphs are located by the exact text we wrote, within a generous window around
 * the caret. Nothing is guessed at: if the text is not found, the entry is reported as
 * un-undoable and left on the journal.
 */
export async function undo(journal, count = 1, { withStyle = true } = {}) {
  let reverted = 0;
  const problems = [];

  for (let n = 0; n < count; n += 1) {
    const entry = journal.pop();
    if (!entry) break;

    try {
      // eslint-disable-next-line no-await-in-loop
      const ok = await Word.run(async (context) => {
        const body = context.document.body;
        let done = 0;

        for (const action of [...entry.actions].reverse()) {
          if (action.type === "reinsert") {
            const target = action.afterText
              ? await findParagraph(context, body, action.afterText)
              : null;
            if (target) {
              const created = target.insertParagraph(action.text, Word.InsertLocation.after);
              const style = withStyle ? toWord(action.style) : null;
              if (style) created.styleBuiltIn = style;
            } else {
              const created = body.insertParagraph(action.text, Word.InsertLocation.start);
              const style = withStyle ? toWord(action.style) : null;
              if (style) created.styleBuiltIn = style;
            }
            await context.sync();
            done += 1;
            continue;
          }

          const paragraph = await findParagraph(context, body, action.after);
          if (!paragraph) continue;

          if (action.type === "remove") {
            paragraph.delete();
          } else {
            paragraph.insertText(action.before, Word.InsertLocation.replace);
            const style = withStyle ? toWord(action.style) : null;
            if (style) paragraph.styleBuiltIn = style;
          }
          await context.sync();
          done += 1;
        }
        return done > 0;
      });

      if (ok) reverted += 1;
      else problems.push(`не найден изменённый текст (${entry.label})`);
    } catch (error) {
      problems.push(describeError(error));
    }
  }

  if (reverted === 0) {
    return {
      ok: false,
      applied: 0,
      conflicts: [],
      error: problems[0] || "нечего отменять",
    };
  }
  return { ok: true, applied: reverted, conflicts: [], error: "" };
}

/**
 * Find the paragraph whose text equals `text`, searching outwards from the caret.
 *
 * Empty paragraphs cannot be searched for, so those are matched by scanning the window
 * around the caret directly.
 */
async function findParagraph(context, body, text) {
  const target = cleanText(text);

  if (target.length && target.length <= MAX_SEARCH) {
    const results = body.search(escapeForSearch(target), { matchCase: true });
    results.load("items");
    await context.sync();
    if (results.items.length) {
      const paragraph = results.items[0].paragraphs.getFirst();
      paragraph.load("text");
      await context.sync();
      if (cleanText(paragraph.text) === target) return paragraph;
    }
  }

  // Fall back to a positional scan: covers empty paragraphs and very long ones.
  const nearby = await resolveIds(
    context,
    ["P-3", "P-2", "P-1", "P0", "P+1", "P+2", "P+3"],
    false,
  );
  for (const paragraph of nearby.values()) {
    if (cleanText(paragraph.text) === target) return paragraph;
  }
  return null;
}

export { readWindow };
