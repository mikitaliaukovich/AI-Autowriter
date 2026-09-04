# AI-Autowriter

Research into a voice-driven "smart auto-writer" for word processors: an
extension that listens continuously, distinguishes writing from thinking aloud,
and executes spoken edits against the document.

## Status

Research only — no implementation yet.

**Verdict: buildable, as a Microsoft Word on the web add-in backed by a localhost
sidecar running local speech and language models.** Google Docs was investigated
and ruled out (its add-ons cannot access the microphone).

## Documents

- **[Feasibility report](docs/feasibility-voice-autowriter.md)** — platform
  analysis, model selection for Russian, architecture, the editing model, risks,
  and a phased plan.
- **[Краткое резюме на русском](docs/ru/README.md)** — summary and
  recommendation in Russian.

## The five acceptance criteria

1. Always listening.
2. Thoughts spoken aloud are not written down unless the user says so.
3. Spoken requests to replace, remove or adjust wording are executed —
   including inline self-correction ("She was a fairy. No, edit to she was a
   witch" must leave "She was a witch").
4. Noise, side-talk and commands never appear in the text.
5. After a mid-sentence pause, the system continues the sentence.
