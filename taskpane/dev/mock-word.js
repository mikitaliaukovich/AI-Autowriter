/**
 * An in-memory stand-in for Office.js, for exercising the task pane without Word.
 *
 * It implements only the surface `context.js`, `apply.js` and `main.js` actually use,
 * but it implements it with the same *semantics* that make Office.js awkward: proxy
 * objects whose properties are undefined until `load()` + `sync()`, null objects at the
 * document edges, and search results as a collection needing its own sync.
 *
 * This validates our logic — walking, hashing, conflict detection, the journal — not
 * Word itself. Real Word behaviour still has to be checked in Word.
 */

const NOT_LOADED = Symbol("not loaded");

class MockDoc {
  constructor(paragraphs) {
    this.paragraphs = paragraphs.map((p, i) =>
      typeof p === "string" ? { text: p, style: "Normal", id: i } : { style: "Normal", ...p, id: i },
    );
    this._nextId = this.paragraphs.length;
    this.caret = { paragraph: 0, offset: 0 };
  }

  newId() {
    return this._nextId++;
  }

  indexOfId(id) {
    return this.paragraphs.findIndex((p) => p.id === id);
  }

  toText() {
    return this.paragraphs.map((p) => `${p.style === "Normal" ? "" : `[${p.style}] `}${p.text}`);
  }
}

class Batch {
  constructor(doc) {
    this.doc = doc;
    this.queue = [];
    this.document = new MockDocument(this);
  }

  defer(fn) {
    this.queue.push(fn);
  }

  async sync() {
    const queued = this.queue;
    this.queue = [];
    for (const fn of queued) fn();
  }
}

class MockDocument {
  constructor(batch) {
    this.batch = batch;
    this.body = new MockBody(batch);
    this.properties = new Loadable(batch, () => ({ title: "Mock" }));
  }

  getSelection() {
    return new MockRange(this.batch, {
      kind: "selection",
      paragraphIndex: () => this.batch.doc.caret.paragraph,
      offset: () => this.batch.doc.caret.offset,
    });
  }
}

class Loadable {
  constructor(batch, resolve) {
    this.batch = batch;
    this._resolve = resolve;
  }

  load() {
    this.batch.defer(() => Object.assign(this, this._resolve()));
  }
}

class MockBody {
  constructor(batch) {
    this.batch = batch;
  }

  insertParagraph(text, location) {
    const record = { id: this.batch.doc.newId(), text, style: "Normal" };
    this.batch.defer(() => {
      if (location === "Start") this.batch.doc.paragraphs.unshift(record);
      else this.batch.doc.paragraphs.push(record);
    });
    return new MockParagraph(this.batch, record.id);
  }

  search(pattern, options) {
    return new MockSearchResults(this.batch, null, pattern, options);
  }
}

class MockSearchResults {
  constructor(batch, paragraphId, pattern, options = {}) {
    this.batch = batch;
    this.paragraphId = paragraphId;
    this.pattern = String(pattern).replace(/\^\^/g, "^");
    this.options = options;
    this.items = NOT_LOADED;
  }

  load() {
    this.batch.defer(() => {
      const scope =
        this.paragraphId === null
          ? this.batch.doc.paragraphs
          : this.batch.doc.paragraphs.filter((p) => p.id === this.paragraphId);
      this.items = scope
        .filter((p) => p.text.includes(this.pattern))
        .map((p) => new MockFoundRange(this.batch, p.id, this.pattern));
    });
  }
}

class MockFoundRange {
  constructor(batch, paragraphId, pattern) {
    this.batch = batch;
    this.paragraphId = paragraphId;
    this.pattern = pattern;
  }

  insertText(text, location) {
    if (location !== "Replace") throw new Error(`unsupported location ${location}`);
    this.batch.defer(() => {
      const record = this.batch.doc.paragraphs[this.batch.doc.indexOfId(this.paragraphId)];
      record.text = record.text.replace(this.pattern, text);
    });
  }

  get paragraphs() {
    return {
      getFirst: () => new MockParagraph(this.batch, this.paragraphId),
    };
  }
}

class MockParagraph {
  constructor(batch, id) {
    this.batch = batch;
    this.id = id;
    this.isNullObject = id === null;
    this.text = NOT_LOADED;
    // `styleBuiltIn` is a prototype accessor that writes through to the document, so
    // it is deliberately NOT initialised here; `load()` populates it.
  }

  _record() {
    const index = this.batch.doc.indexOfId(this.id);
    return index < 0 ? null : this.batch.doc.paragraphs[index];
  }

  load(properties) {
    this.batch.defer(() => {
      const record = this._record();
      if (!record) {
        this.isNullObject = true;
        this.text = "";
        return;
      }
      if (properties.includes("text")) this.text = record.text;
      if (properties.includes("styleBuiltIn")) this.styleBuiltIn = record.style;
      this.isNullObject = false;
    });
  }

  _sibling(delta) {
    const index = this.batch.doc.indexOfId(this.id);
    const neighbour = this.batch.doc.paragraphs[index + delta];
    return new MockParagraph(this.batch, neighbour ? neighbour.id : null);
  }

  getPreviousOrNullObject() {
    return this._sibling(-1);
  }

  getNextOrNullObject() {
    return this._sibling(1);
  }

  insertText(text, location) {
    this.batch.defer(() => {
      const record = this._record();
      if (!record) return;
      if (location === "End") record.text += text;
      else if (location === "Replace") record.text = text;
      else throw new Error(`unsupported location ${location}`);
    });
    return this;
  }

  insertParagraph(text, location) {
    if (location !== "After") throw new Error(`unsupported location ${location}`);
    const record = { id: this.batch.doc.newId(), text, style: "Normal" };
    const created = new MockParagraph(this.batch, record.id);
    this.batch.defer(() => {
      const index = this.batch.doc.indexOfId(this.id);
      this.batch.doc.paragraphs.splice(index + 1, 0, record);
    });
    return created;
  }

  delete() {
    this.batch.defer(() => {
      const index = this.batch.doc.indexOfId(this.id);
      if (index >= 0) this.batch.doc.paragraphs.splice(index, 1);
    });
  }

  search(pattern, options) {
    return new MockSearchResults(this.batch, this.id, pattern, options);
  }

  getRange(which) {
    return new MockRange(this.batch, { kind: "paragraph", id: this.id, which });
  }

  select() {
    this.batch.defer(() => {
      const record = this._record();
      if (!record) return;
      this.batch.doc.caret = { paragraph: this.batch.doc.indexOfId(this.id), offset: record.text.length };
    });
  }
}

// `styleBuiltIn` on an existing paragraph must write through to the document.
Object.defineProperty(MockParagraph.prototype, "styleBuiltIn", {
  configurable: true,
  get() {
    return this._style ?? NOT_LOADED;
  },
  set(value) {
    this._style = value;
    this.batch.defer(() => {
      const record = this._record();
      if (record) record.style = value;
    });
  },
});

class MockRange {
  constructor(batch, spec) {
    this.batch = batch;
    this.spec = spec;
    this.text = NOT_LOADED;
  }

  get paragraphs() {
    return {
      getFirst: () => {
        const index =
          this.spec.kind === "selection" ? this.spec.paragraphIndex() : this.batch.doc.indexOfId(this.spec.id);
        const record = this.batch.doc.paragraphs[index];
        return new MockParagraph(this.batch, record ? record.id : null);
      },
    };
  }

  getRange() {
    return this;
  }

  expandTo() {
    // Used only to measure the caret offset inside the anchor paragraph.
    const range = new MockRange(this.batch, { kind: "head" });
    range.load = () => {
      this.batch.defer(() => {
        range.text = "x".repeat(this.batch.doc.caret.offset);
      });
    };
    return range;
  }

  load() {
    this.batch.defer(() => {
      if (this.spec.kind === "selection") {
        this.text = "";
      } else {
        const record = this.batch.doc.paragraphs[this.batch.doc.indexOfId(this.spec.id)];
        this.text = record ? record.text : "";
      }
    });
  }

  insertText(text, location) {
    if (location !== "Replace") throw new Error(`unsupported location ${location}`);
    this.batch.defer(() => {
      const { paragraph, offset } = this.batch.doc.caret;
      const record = this.batch.doc.paragraphs[paragraph];
      if (!record) return;
      record.text = record.text.slice(0, offset) + text + record.text.slice(offset);
      this.batch.doc.caret = { paragraph, offset: offset + text.length };
    });
  }

  select() {
    this.batch.defer(() => {
      if (this.spec.kind !== "paragraph") return;
      const index = this.batch.doc.indexOfId(this.spec.id);
      const record = this.batch.doc.paragraphs[index];
      if (!record) return;
      this.batch.doc.caret = {
        paragraph: index,
        offset: this.spec.which === "Start" ? 0 : record.text.length,
      };
    });
  }
}

/** Install the mock globals. Returns the document so tests can inspect it. */
export function installMockWord(paragraphs, caret = { paragraph: 0, offset: 0 }) {
  const doc = new MockDoc(paragraphs);
  doc.caret = caret;

  globalThis.Word = {
    InsertLocation: { after: "After", before: "Before", start: "Start", end: "End", replace: "Replace" },
    Style: {},
    async run(callback) {
      const batch = new Batch(doc);
      const result = await callback(batch);
      await batch.sync();
      return result;
    },
  };

  globalThis.Office = {
    HostType: { Word: "Word" },
    EventType: { DocumentSelectionChanged: "DocumentSelectionChanged" },
    AsyncResultStatus: { Succeeded: "succeeded", Failed: "failed" },
    context: {
      platform: "PC",
      diagnostics: { version: "16.0.0" },
      requirements: { isSetSupported: () => true },
      document: { addHandlerAsync: (_type, _handler, cb) => cb?.({ status: "succeeded" }) },
    },
    onReady: (cb) => cb({ host: "Word" }),
  };

  return doc;
}
