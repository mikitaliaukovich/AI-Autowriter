/**
 * Browser tests for the task pane's Word logic, run against the mock in `mock-word.js`.
 *
 * These cover our own logic — window walking, hashing, conflict detection, op dispatch
 * and the undo journal. Real Office.js behaviour still has to be verified in Word; what
 * this catches is everything that would otherwise only surface there.
 */

import { installMockWord } from "./mock-word.js";

const results = [];
let currentSuite = "";

function suite(name) {
  currentSuite = name;
}

async function test(name, fn) {
  try {
    await fn();
    results.push({ suite: currentSuite, name, ok: true });
  } catch (error) {
    results.push({ suite: currentSuite, name, ok: false, error: error.message || String(error) });
  }
}

function assert(condition, message) {
  if (!condition) throw new Error(message || "assertion failed");
}

function equal(actual, expected, message) {
  const a = JSON.stringify(actual);
  const b = JSON.stringify(expected);
  if (a !== b) throw new Error(`${message || "not equal"}\n  actual:   ${a}\n  expected: ${b}`);
}

export async function runTests() {
  results.length = 0;

  // The modules under test read the Word globals at call time, so install first.
  installMockWord(["one"]);
  const { readWindow, hashText, cleanText } = await import("../src/context.js");
  const { applyOps, undo } = await import("../src/apply.js");
  const { Journal } = await import("../src/journal.js");

  const setup = (paragraphs, caret) => installMockWord(paragraphs, caret);
  const texts = (doc) => doc.paragraphs.map((p) => p.text);
  const styles = (doc) => doc.paragraphs.map((p) => p.style);

  async function withExpect(doc, ops) {
    // Attach the hashes the service would have attached from a context read.
    const window = await readWindow({ before: 6, after: 2 });
    return ops.map((op) => {
      if (!op.id) return op;
      const paragraph = window.paragraphs.find((p) => p.id === op.id);
      return paragraph ? { ...op, expect: paragraph.hash } : op;
    });
  }

  /* --- context window ---------------------------------------------------------- */

  suite("context window");

  await test("reads the requested number of paragraphs either side", async () => {
    setup(["a", "b", "c", "d", "e", "f"], { paragraph: 3, offset: 1 });
    const window = await readWindow({ before: 2, after: 2 });
    equal(window.paragraphs.map((p) => p.id), ["P-2", "P-1", "P0", "P+1", "P+2"]);
    equal(window.paragraphs.map((p) => p.text), ["b", "c", "d", "e", "f"]);
  });

  await test("stops cleanly at the start of the document", async () => {
    setup(["a", "b", "c"], { paragraph: 0, offset: 0 });
    const window = await readWindow({ before: 6, after: 2 });
    equal(window.paragraphs.map((p) => p.id), ["P0", "P+1", "P+2"]);
  });

  await test("stops cleanly at the end of the document", async () => {
    setup(["a", "b", "c"], { paragraph: 2, offset: 1 });
    const window = await readWindow({ before: 6, after: 2 });
    equal(window.paragraphs.map((p) => p.id), ["P-2", "P-1", "P0"]);
  });

  await test("marks the caret only on the anchor paragraph", async () => {
    setup(["alpha", "bravo", "charlie"], { paragraph: 1, offset: 3 });
    const window = await readWindow({ before: 1, after: 1 });
    const anchor = window.paragraphs.find((p) => p.id === "P0");
    equal(anchor.caret, 3, "caret offset");
    assert(window.paragraphs.filter((p) => p.caret !== undefined).length === 1, "only one caret");
  });

  await test("reports whether the caret sits at the paragraph end", async () => {
    setup(["hello"], { paragraph: 0, offset: 5 });
    assert((await readWindow({})).atEndOfParagraph === true, "should be at end");
    setup(["hello"], { paragraph: 0, offset: 2 });
    assert((await readWindow({})).atEndOfParagraph === false, "should not be at end");
  });

  await test("hash is stable and matches the service digest length", async () => {
    const first = await hashText("Мэри стояла у окна.");
    const second = await hashText("Мэри стояла у окна.");
    equal(first, second, "stable");
    assert(first.length === 12, "12 hex characters");
    assert(first !== (await hashText("Мэри стояла у двери.")), "differs for different text");
  });

  await test("strips the trailing paragraph mark", () => {
    equal(cleanText("текст\r"), "текст");
    equal(cleanText("текст\r\n"), "текст");
  });

  /* --- applying ops ------------------------------------------------------------ */

  suite("apply");

  await test("append_to_paragraph writes at the paragraph end", async () => {
    const doc = setup(["Он вышел на улицу и"], { paragraph: 0, offset: 19 });
    const ops = await withExpect(doc, [{ op: "append_to_paragraph", id: "P0", text: " замер." }]);
    const result = await applyOps(ops, new Journal());
    assert(result.ok, result.error);
    equal(texts(doc), ["Он вышел на улицу и замер."]);
  });

  await test("insert_paragraphs_after adds paragraphs in order with styles", async () => {
    const doc = setup(["Мэри молчала."], { paragraph: 0, offset: 13 });
    const ops = await withExpect(doc, [
      {
        op: "insert_paragraphs_after",
        id: "P0",
        paragraphs: [
          { text: "— Почему ты это сделал?", style: "normal" },
          { text: "Глава вторая", style: "heading1" },
        ],
      },
    ]);
    const result = await applyOps(ops, new Journal());
    assert(result.ok, result.error);
    equal(texts(doc), ["Мэри молчала.", "— Почему ты это сделал?", "Глава вторая"]);
    equal(styles(doc), ["Normal", "Normal", "Heading1"]);
  });

  await test("replace_paragraph swaps text and style", async () => {
    const doc = setup(["старый текст"], { paragraph: 0, offset: 0 });
    const ops = await withExpect(doc, [
      { op: "replace_paragraph", id: "P0", text: "новый текст", style: "quote" },
    ]);
    assert((await applyOps(ops, new Journal())).ok);
    equal(texts(doc), ["новый текст"]);
    equal(styles(doc), ["Quote"]);
  });

  await test("replace_in_paragraph replaces an exact fragment", async () => {
    const doc = setup(["Мэри была очень злая и кричала."], { paragraph: 0, offset: 0 });
    const ops = await withExpect(doc, [
      { op: "replace_in_paragraph", id: "P0", find: "очень злая", replace: "молчалива" },
    ]);
    assert((await applyOps(ops, new Journal())).ok);
    equal(texts(doc), ["Мэри была молчалива и кричала."]);
  });

  await test("replace_in_paragraph reports a missing fragment instead of guessing", async () => {
    const doc = setup(["Мэри молчала."], { paragraph: 0, offset: 0 });
    const ops = await withExpect(doc, [
      { op: "replace_in_paragraph", id: "P0", find: "которого там нет", replace: "x" },
    ]);
    const result = await applyOps(ops, new Journal());
    assert(!result.ok, "should not report success");
    assert(result.conflicts.length === 1, "one conflict");
    assert(/не найден/.test(result.conflicts[0].reason), result.conflicts[0].reason);
    equal(texts(doc), ["Мэри молчала."], "document untouched");
  });

  await test("delete_paragraph removes the right paragraph", async () => {
    const doc = setup(["a", "b", "c"], { paragraph: 1, offset: 0 });
    const ops = await withExpect(doc, [{ op: "delete_paragraph", id: "P0" }]);
    assert((await applyOps(ops, new Journal())).ok);
    equal(texts(doc), ["a", "c"]);
  });

  await test("set_style changes only the style", async () => {
    const doc = setup(["Глава вторая"], { paragraph: 0, offset: 0 });
    const ops = await withExpect(doc, [{ op: "set_style", id: "P0", style: "heading1" }]);
    assert((await applyOps(ops, new Journal())).ok);
    equal(texts(doc), ["Глава вторая"]);
    equal(styles(doc), ["Heading1"]);
  });

  await test("addresses a paragraph behind the caret", async () => {
    const doc = setup(["первый", "второй", "третий"], { paragraph: 2, offset: 0 });
    const ops = await withExpect(doc, [{ op: "replace_paragraph", id: "P-1", text: "ВТОРОЙ" }]);
    assert((await applyOps(ops, new Journal())).ok);
    equal(texts(doc), ["первый", "ВТОРОЙ", "третий"]);
  });

  await test("noop-only batches are a successful no-change", async () => {
    const doc = setup(["текст"], { paragraph: 0, offset: 0 });
    const result = await applyOps([{ op: "noop", reason: "не относится" }], new Journal());
    assert(result.ok && result.applied === 0, "no-op");
    equal(texts(doc), ["текст"]);
  });

  /* --- the anchor follows the dictation ----------------------------------------- */

  suite("anchor");

  await test("caret moves to the end of appended text", async () => {
    const doc = setup(["Он вышел"], { paragraph: 0, offset: 8 });
    await applyOps(await withExpect(doc, [{ op: "append_to_paragraph", id: "P0", text: " на улицу." }]), new Journal());
    equal(doc.caret, { paragraph: 0, offset: "Он вышел на улицу.".length });
  });

  await test("caret moves into the last inserted paragraph", async () => {
    const doc = setup(["Мэри молчала.", "хвост"], { paragraph: 0, offset: 13 });
    await applyOps(
      await withExpect(doc, [
        {
          op: "insert_paragraphs_after",
          id: "P0",
          paragraphs: [{ text: "— Первая.", style: "normal" }, { text: "— Вторая.", style: "normal" }],
        },
      ]),
      new Journal(),
    );
    equal(doc.caret, { paragraph: 2, offset: "— Вторая.".length }, "caret should sit after the last insert");
  });

  await test("the next context window is centred on the new caret", async () => {
    const doc = setup(["Мэри молчала.", "хвост"], { paragraph: 0, offset: 13 });
    await applyOps(
      await withExpect(doc, [
        { op: "insert_paragraphs_after", id: "P0", paragraphs: [{ text: "— Реплика.", style: "normal" }] },
      ]),
      new Journal(),
    );
    const window = await readWindow({ before: 2, after: 1 });
    const anchor = window.paragraphs.find((p) => p.id === "P0");
    equal(anchor.text, "— Реплика.", "the freshly written paragraph is the new anchor");
  });

  /* --- optimistic concurrency --------------------------------------------------- */

  suite("conflict detection");

  await test("rejects the batch when the paragraph changed after the read", async () => {
    const doc = setup(["исходный текст"], { paragraph: 0, offset: 0 });
    const ops = await withExpect(doc, [{ op: "replace_paragraph", id: "P0", text: "новый" }]);
    doc.paragraphs[0].text = "пользователь напечатал своё";

    const result = await applyOps(ops, new Journal());
    assert(!result.ok, "must not apply");
    equal(result.conflicts.length, 1);
    assert(/изменил/.test(result.conflicts[0].reason), result.conflicts[0].reason);
    equal(texts(doc), ["пользователь напечатал своё"], "user's text preserved");
  });

  await test("rejects an id that is outside the window", async () => {
    const doc = setup(["a", "b"], { paragraph: 0, offset: 0 });
    const result = await applyOps([{ op: "replace_paragraph", id: "P-5", text: "x" }], new Journal());
    assert(!result.ok, "must not apply");
    assert(/вне окна/.test(result.conflicts[0].reason), result.conflicts[0].reason);
    equal(texts(doc), ["a", "b"]);
  });

  await test("applies when no expect hash is supplied", async () => {
    const doc = setup(["текст"], { paragraph: 0, offset: 0 });
    assert((await applyOps([{ op: "append_to_paragraph", id: "P0", text: "!" }], new Journal())).ok);
    equal(texts(doc), ["текст!"]);
  });

  /* --- undo --------------------------------------------------------------------- */

  suite("undo");

  await test("reverses an append", async () => {
    const doc = setup(["Он вышел"], { paragraph: 0, offset: 8 });
    const journal = new Journal();
    await applyOps(await withExpect(doc, [{ op: "append_to_paragraph", id: "P0", text: " на улицу." }]), journal);
    equal(texts(doc), ["Он вышел на улицу."]);

    const result = await undo(journal, 1);
    assert(result.ok, result.error);
    equal(texts(doc), ["Он вышел"]);
    assert(!journal.canUndo(), "journal emptied");
  });

  await test("reverses inserted paragraphs", async () => {
    const doc = setup(["начало"], { paragraph: 0, offset: 6 });
    const journal = new Journal();
    await applyOps(
      await withExpect(doc, [
        {
          op: "insert_paragraphs_after",
          id: "P0",
          paragraphs: [{ text: "— Первая реплика.", style: "normal" }, { text: "— Вторая реплика.", style: "normal" }],
        },
      ]),
      journal,
    );
    equal(texts(doc), ["начало", "— Первая реплика.", "— Вторая реплика."]);

    assert((await undo(journal, 1)).ok);
    equal(texts(doc), ["начало"]);
  });

  await test("reverses a replacement including its style", async () => {
    const doc = setup(["обычный абзац"], { paragraph: 0, offset: 0 });
    const journal = new Journal();
    await applyOps(
      await withExpect(doc, [{ op: "replace_paragraph", id: "P0", text: "Глава третья", style: "heading1" }]),
      journal,
    );
    equal(styles(doc), ["Heading1"]);

    assert((await undo(journal, 1)).ok);
    equal(texts(doc), ["обычный абзац"]);
    equal(styles(doc), ["Normal"]);
  });

  await test("reverses a deletion, back in position", async () => {
    const doc = setup(["первый", "второй", "третий"], { paragraph: 1, offset: 0 });
    const journal = new Journal();
    await applyOps(await withExpect(doc, [{ op: "delete_paragraph", id: "P0" }]), journal);
    equal(texts(doc), ["первый", "третий"]);

    assert((await undo(journal, 1)).ok);
    equal(texts(doc), ["первый", "второй", "третий"]);
  });

  await test("undoes several edits newest-first", async () => {
    const doc = setup(["база"], { paragraph: 0, offset: 4 });
    const journal = new Journal();
    await applyOps(await withExpect(doc, [{ op: "append_to_paragraph", id: "P0", text: " один" }]), journal);
    await applyOps(await withExpect(doc, [{ op: "append_to_paragraph", id: "P0", text: " два" }]), journal);
    equal(texts(doc), ["база один два"]);

    await undo(journal, 1);
    equal(texts(doc), ["база один"]);
    await undo(journal, 1);
    equal(texts(doc), ["база"]);
  });

  await test("revert op routes through the journal", async () => {
    const doc = setup(["текст"], { paragraph: 0, offset: 5 });
    const journal = new Journal();
    await applyOps(await withExpect(doc, [{ op: "append_to_paragraph", id: "P0", text: " ещё" }]), journal);
    const result = await applyOps([{ op: "revert", count: 1 }], journal);
    assert(result.ok, result.error);
    equal(texts(doc), ["текст"]);
  });

  await test("reports honestly when there is nothing to undo", async () => {
    setup(["текст"], { paragraph: 0, offset: 0 });
    const result = await undo(new Journal(), 1);
    assert(!result.ok, "should fail");
    assert(/нечего отменять/.test(result.error), result.error);
  });

  await test("journal is capped so it cannot grow without bound", async () => {
    const journal = new Journal(3);
    for (let i = 0; i < 10; i += 1) journal.record([{ type: "restore", after: `a${i}`, before: "", style: "normal" }]);
    equal(journal.size, 3);
  });

  return results;
}
