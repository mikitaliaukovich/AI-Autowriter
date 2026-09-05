/**
 * Undo journal.
 *
 * Office.js cannot drive Word's own undo stack, and "мне не нравится, отмени" has to be
 * reliable — it is one of the things the user was promised. So every batch records how
 * to reverse itself.
 *
 * Entries are addressed by *text*, not by a paragraph proxy: proxies do not survive
 * across `Word.run` calls, and paragraph ids are positions relative to a caret that has
 * since moved. Undo therefore re-reads a window and matches the paragraph it wrote. In
 * practice an undo follows its edit within seconds, so the paragraph is still in view;
 * if it is not, the user is told rather than having some other paragraph mangled.
 */

const LIMIT = 20;

export class Journal {
  constructor(limit = LIMIT) {
    this.limit = limit;
    /** @type {{id: number, at: number, label: string, actions: object[]}[]} */
    this.entries = [];
    this._nextId = 1;
    this.onChange = null;
  }

  get size() {
    return this.entries.length;
  }

  canUndo() {
    return this.entries.length > 0;
  }

  /**
   * @param {object[]} actions reversal steps, in the order the edits were made
   * @param {string} label short human-readable description for the log
   */
  record(actions, label = "правка") {
    if (!actions.length) return null;
    const entry = { id: this._nextId++, at: Date.now(), label, actions };
    this.entries.push(entry);
    while (this.entries.length > this.limit) this.entries.shift();
    this.onChange?.(this);
    return entry;
  }

  pop() {
    const entry = this.entries.pop() ?? null;
    if (entry) this.onChange?.(this);
    return entry;
  }

  clear() {
    this.entries = [];
    this.onChange?.(this);
  }
}

/** A paragraph we overwrote: put its old text and style back. */
export function restoreAction(afterText, beforeText, style) {
  return { type: "restore", after: afterText, before: beforeText, style };
}

/** A paragraph we created: delete it again. */
export function removeAction(afterText) {
  return { type: "remove", after: afterText };
}

/** A paragraph we deleted: recreate it after `afterText`. */
export function reinsertAction(text, style, afterText) {
  return { type: "reinsert", text, style, afterText };
}
