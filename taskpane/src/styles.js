/**
 * Paragraph style mapping.
 *
 * The wire protocol uses language-independent keys ("heading1") while Word's
 * `styleBuiltIn` property takes the enum's string values ("Heading1"). We never touch
 * `paragraph.style`, which takes *localised* names — on a Russian Word UI that is
 * "Заголовок 1", and hard-coding either language would break the other.
 */

/** @type {Record<string, string>} wire key -> Word.Style value */
export const TO_WORD = {
  normal: "Normal",
  heading1: "Heading1",
  heading2: "Heading2",
  heading3: "Heading3",
  quote: "Quote",
  intenseQuote: "IntenseQuote",
  listParagraph: "ListParagraph",
};

/** @type {Record<string, string>} Word.Style value -> wire key */
export const FROM_WORD = Object.fromEntries(
  Object.entries(TO_WORD).map(([key, value]) => [value, key]),
);

/** Normalise a style coming from Word into a wire key, defaulting to "normal". */
export function fromWord(value) {
  return FROM_WORD[value] || "normal";
}

/** Translate a wire key into a Word style value, or null if we do not support it. */
export function toWord(key) {
  return TO_WORD[key] || null;
}
