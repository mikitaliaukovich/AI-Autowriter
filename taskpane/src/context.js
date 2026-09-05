/**
 * Reading the context window around the caret.
 *
 * The anchor is Word's own selection, which satisfies both halves of the requirement
 * with one mechanism: after each edit we select the end of what was written, so the
 * window follows the dictation; and when the user clicks somewhere else, the window is
 * simply wherever they clicked.
 *
 * The walk uses `getPreviousOrNullObject` / `getNextOrNullObject` one step at a time
 * rather than loading `body.paragraphs`. Loading every paragraph would marshal the
 * whole manuscript on every caret move; this is O(window size) regardless of book
 * length. Both directions advance in the same sync, so a 6-back/2-ahead window costs
 * six round trips, not eight.
 */

import { fromWord } from "./styles.js";

/** Word appends a paragraph mark to `Paragraph.text` in some hosts. */
const TRAILING = /[\r\n\v\f]+$/;

export function cleanText(text) {
  return String(text ?? "").replace(TRAILING, "");
}

/** Short digest, matching the service's `protocol.text_hash`. */
export async function hashText(text) {
  const bytes = new TextEncoder().encode(text);
  const digest = await crypto.subtle.digest("SHA-1", bytes);
  return Array.from(new Uint8Array(digest))
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("")
    .slice(0, 12);
}

/** Load a paragraph's text, and its style when this Word build supports it. */
function loadParagraph(paragraph, withStyle) {
  paragraph.load(withStyle ? "text,styleBuiltIn,isNullObject" : "text,isNullObject");
}

/**
 * Walk outwards from the anchor paragraph.
 * @returns {{before: object[], after: object[]}} proxies, nearest-first
 */
async function walk(context, anchor, before, after, withStyle) {
  const backward = [];
  const forward = [];
  let back = anchor;
  let fwd = anchor;
  let backDone = false;
  let fwdDone = false;

  for (let step = 0; step < Math.max(before, after); step += 1) {
    const wantBack = !backDone && step < before;
    const wantFwd = !fwdDone && step < after;
    if (!wantBack && !wantFwd) break;

    const nextBack = wantBack ? back.getPreviousOrNullObject() : null;
    const nextFwd = wantFwd ? fwd.getNextOrNullObject() : null;
    if (nextBack) loadParagraph(nextBack, withStyle);
    if (nextFwd) loadParagraph(nextFwd, withStyle);
    await context.sync();

    if (nextBack) {
      if (nextBack.isNullObject) backDone = true;
      else {
        backward.push(nextBack);
        back = nextBack;
      }
    }
    if (nextFwd) {
      if (nextFwd.isNullObject) fwdDone = true;
      else {
        forward.push(nextFwd);
        fwd = nextFwd;
      }
    }
  }
  return { before: backward, after: forward };
}

/** Caret offset within the anchor paragraph, or null if it cannot be determined. */
async function caretOffset(context, selection, anchor) {
  try {
    const head = anchor.getRange("Start").expandTo(selection.getRange("Start"));
    head.load("text");
    await context.sync();
    return cleanText(head.text).length;
  } catch {
    // `expandTo` needs WordApi 1.3; without it we simply do not mark the caret.
    return null;
  }
}

async function describe(id, paragraph, withStyle, caret) {
  const text = cleanText(paragraph.text);
  return {
    id,
    text,
    style: withStyle ? fromWord(paragraph.styleBuiltIn) : "normal",
    hash: await hashText(text),
    empty: text.length === 0,
    ...(caret === null || caret === undefined ? {} : { caret }),
  };
}

/**
 * Read the window around the caret.
 *
 * @param {{before: number, after: number, withStyle: boolean}} options
 * @returns {Promise<object>} a `context` message payload
 */
export async function readWindow({ before = 6, after = 2, withStyle = true } = {}) {
  return Word.run(async (context) => {
    const selection = context.document.getSelection();
    const anchor = selection.paragraphs.getFirst();
    loadParagraph(anchor, withStyle);
    selection.load("text");
    const properties = context.document.properties;
    properties.load("title");
    await context.sync();

    const caret = await caretOffset(context, selection, anchor);
    const neighbours = await walk(context, anchor, before, after, withStyle);

    const paragraphs = [];
    for (let i = neighbours.before.length - 1; i >= 0; i -= 1) {
      paragraphs.push(await describe(`P-${i + 1}`, neighbours.before[i], withStyle, null));
    }
    paragraphs.push(await describe("P0", anchor, withStyle, caret ?? 0));
    for (let i = 0; i < neighbours.after.length; i += 1) {
      paragraphs.push(await describe(`P+${i + 1}`, neighbours.after[i], withStyle, null));
    }

    const anchorText = cleanText(anchor.text);
    return {
      paragraphs,
      atEndOfParagraph: caret === null ? true : caret >= anchorText.length,
      selectionText: cleanText(selection.text),
      docTitle: properties.title || "",
    };
  });
}

/**
 * Resolve the window's paragraph ids to live proxies inside an existing batch.
 *
 * Used by `apply`, which must re-resolve rather than reuse proxies from the read:
 * ids are positions relative to the caret, and the user may have typed in between.
 *
 * @returns {Promise<Map<string, object>>}
 */
export async function resolveIds(context, ids, withStyle = true) {
  const wanted = new Set(ids);
  let maxBefore = 0;
  let maxAfter = 0;
  for (const id of wanted) {
    const offset = Number.parseInt(id.slice(1), 10);
    if (Number.isNaN(offset)) continue;
    if (offset < 0) maxBefore = Math.max(maxBefore, -offset);
    if (offset > 0) maxAfter = Math.max(maxAfter, offset);
  }

  const anchor = context.document.getSelection().paragraphs.getFirst();
  loadParagraph(anchor, withStyle);
  await context.sync();

  const neighbours = await walk(context, anchor, maxBefore, maxAfter, withStyle);
  const map = new Map([["P0", anchor]]);
  neighbours.before.forEach((p, i) => map.set(`P-${i + 1}`, p));
  neighbours.after.forEach((p, i) => map.set(`P+${i + 1}`, p));
  return map;
}
